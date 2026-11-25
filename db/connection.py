# # from hdbcli import dbapi
# # from dotenv import load_dotenv
# # import os

# # load_dotenv()

# # def get_connection():
# #     return dbapi.connect(
# #         address=os.getenv("HANA_HOST"),
# #         port=int(os.getenv("HANA_PORT", 443)),
# #         user=os.getenv("HANA_USER"),
# #         password=os.getenv("HANA_PASSWORD"),
# #     )

# #---------------------------------------
# from hdbcli import dbapi
# from dotenv import load_dotenv
# import os
# import inspect

# def debug_log(message):
#     frame = inspect.currentframe().f_back
#     filename = inspect.getframeinfo(frame).filename.split('\\')[-1]
#     function_name = frame.f_code.co_name
#     line_number = frame.f_lineno
#     print(f"[DEBUG] {filename}::{function_name}:{line_number} - {message}")

# load_dotenv()
# debug_log("Environment variables loaded from .env file")

# def get_connection():
#     debug_log("Attempting to establish database connection")
    
#     hana_host = os.getenv("HANA_HOST")
#     hana_port = int(os.getenv("HANA_PORT", 443))
#     hana_user = os.getenv("HANA_USER")
#     debug_log(f"Connection parameters - Host: {hana_host}, Port: {hana_port}, User: {hana_user}")
    
#     connection = dbapi.connect(
#         address=hana_host,
#         port=hana_port,
#         user=hana_user,
#         password=os.getenv("HANA_PASSWORD"),
#     )
#     debug_log("Database connection established successfully")
    
#     return connection

import os
import threading
import time
import pymysql
from dotenv import load_dotenv
from utils.logger import debug_log, warning_log, log_duration

load_dotenv(override=True)

debug_log(f"DB connection module loaded from {__file__} (DB_USER={os.getenv('DB_USER')}, "
          f"DB_HOST={os.getenv('DB_HOST') or '127.0.0.1'}, DB_NAME={os.getenv('DB_NAME')})")

# ---------------------------------------------------------------------------
# Connection pool
#   Opening a connection to the remote DB takes ~1.5 s, and the app opens one for almost
#   every query. Connections are therefore kept and reused: get_connection() hands out an
#   idle one when available, and conn.close() puts it back (after a rollback, so the next
#   user always starts a fresh transaction and sees current data).
#   Callers keep using get_connection() / conn.close() exactly as before.
# ---------------------------------------------------------------------------
_POOL_SIZE = int(os.getenv("DB_POOL_SIZE", "5"))            # idle connections kept
_POOL_MAX_IDLE_SECONDS = 300                                 # drop connections idle longer than this
_PING_AFTER_IDLE_SECONDS = 30                                # check a connection idle longer than this
_pool = []                                                   # [(raw connection, idle since)]
_pool_lock = threading.Lock()


def _new_raw_connection():
    host = os.getenv("DB_HOST") or "127.0.0.1"
    port = int(os.getenv("DB_PORT", 3306))
    target = f"{os.getenv('DB_USER')}@{host}:{port}/{os.getenv('DB_NAME')}"
    with log_duration(f"Open new DB connection to {target}", level="DEBUG", slow_ms=5000):
        return pymysql.connect(
            host=host,
            port=port,
            user=os.getenv("DB_USER"),
            password=os.getenv("DB_PASSWORD"),
            database=os.getenv("DB_NAME"),
            autocommit=False,
            # Work in IST regardless of the DB server's own clock setting: NOW() / CURRENT_TIMESTAMP
            # defaults are written in IST and TIMESTAMP columns are read back in IST.
            init_command="SET time_zone = '{}'".format(os.getenv("DB_SESSION_TIMEZONE", "+05:30")),
        )


def _discard(raw):
    try:
        raw.close()
    except Exception as e:
        debug_log(f"Ignoring error while closing discarded DB connection: {e}")


class PooledConnection:
    """Acts like a pymysql connection; close() returns it to the pool instead of disconnecting."""

    def __init__(self, raw):
        self._raw = raw

    def close(self):
        raw, self._raw = self._raw, None
        if raw is None:
            return                                   # already closed
        try:
            raw.rollback()                           # end any open transaction
        except Exception as e:
            warning_log(f"Rollback failed on DB connection; discarding broken connection: {e}")
            _discard(raw)                            # broken connection: don't reuse it
            return
        with _pool_lock:
            if len(_pool) < _POOL_SIZE:
                _pool.append((raw, time.monotonic()))
                return
        debug_log(f"DB pool full ({_POOL_SIZE} idle); closing returned connection")
        _discard(raw)

    def __getattr__(self, name):
        raw = self.__dict__.get("_raw")
        if raw is None:
            raise pymysql.err.InterfaceError(0, "Connection is closed")
        return getattr(raw, name)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def __del__(self):
        # a connection that was never closed still goes back to the pool
        try:
            self.close()
        except Exception:
            pass


try:
    from flask import g, has_app_context
except ImportError:                                          # used outside Flask (e.g. scripts)
    g = None

    def has_app_context():
        return False


class _BorrowedConnection:
    """A request's shared connection handed to one caller; close() leaves it open for the next
    caller in the same request. The request's teardown returns it to the pool (with rollback)."""

    def __init__(self, shared):
        self._shared = shared

    def close(self):
        self._shared = None

    def __getattr__(self, name):
        shared = self.__dict__.get("_shared")
        if shared is None:
            raise pymysql.err.InterfaceError(0, "Connection is closed")
        return getattr(shared, name)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def get_connection():
    """A DB connection. Inside a Flask request all calls share one pooled connection (fewer
    round trips to the remote DB); elsewhere each call gets its own pooled connection."""
    if has_app_context():
        shared = g.get("_db_conn")
        if shared is None or shared.__dict__.get("_raw") is None:
            shared = _checkout()
            g._db_conn = shared
        return _BorrowedConnection(shared)
    return _checkout()


def release_request_connection(exc=None):
    """Flask teardown hook: return the request's shared connection to the pool."""
    if not has_app_context():
        return
    shared = g.pop("_db_conn", None)
    if shared is not None:
        shared.close()


def warm_up():
    """Open one connection in the background so the first page doesn't wait for the handshake."""
    def _open():
        try:
            _checkout().close()
            debug_log("DB warm-up connection ready")
        except Exception as e:
            warning_log(f"DB warm-up failed: {e}")
    threading.Thread(target=_open, daemon=True).start()


def _checkout():
    raw = None
    with _pool_lock:
        while _pool:
            candidate, idle_since = _pool.pop()
            if time.monotonic() - idle_since > _POOL_MAX_IDLE_SECONDS:
                debug_log(f"Discarding DB connection idle > {_POOL_MAX_IDLE_SECONDS}s")
                _discard(candidate)
                continue
            raw = (candidate, idle_since)
            break
    if raw is not None:
        candidate, idle_since = raw
        if time.monotonic() - idle_since <= _PING_AFTER_IDLE_SECONDS:
            debug_log("Reusing pooled DB connection")
            return PooledConnection(candidate)
        try:
            candidate.ping(reconnect=True)           # reconnect re-runs init_command (time zone)
            debug_log("Reusing pooled DB connection after ping")
            return PooledConnection(candidate)
        except Exception as e:
            warning_log(f"Pooled DB connection failed ping; discarding and opening a new one: {e}")
            _discard(candidate)
    return PooledConnection(_new_raw_connection())