"""
Central logging for Hire Assist AI (admin app, candidate app, email pipeline and scripts).

Usage (same names as before):
    from utils.logger import debug_log, info_log, warning_log, error_log, log_duration

    info_log("Shortlisted CV 1a2b3c")
    try:
        ...
    except Exception as e:
        error_log(f"Saving CV failed: {e}")        # traceback is attached automatically
    with log_duration("LLM question generation"):
        ...                                         # logs how long it took (and failures)

Every line looks like:
    [2026-11-17 22:21:57.123 IST] [INFO] main.py::send_assessment:109 - Assessment email sent ...

Output goes to the console and to daily files in ./logs (one set per program):
    logs/<program>_<YYYY-MM-DD>.log          everything at LOG_LEVEL and above
    logs/<program>_errors_<YYYY-MM-DD>.log   warnings and errors only (with tracebacks)

Environment variables (optional, e.g. in .env):
    LOG_LEVEL            DEBUG | INFO | WARNING | ERROR      (default DEBUG)
    LOG_DIR              folder for the log files            (default <project>/logs)
    LOG_RETENTION_DAYS   delete log files older than this    (default 30)
    LOG_TO_FILE          0 to log to the console only        (default 1)
"""
import logging
import os
import re
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime

try:
    from zoneinfo import ZoneInfo
    _IST = ZoneInfo("Asia/Kolkata")
except Exception:                                   # tzdata missing: fall back to local time
    _IST = None

_PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_LOGGER_NAME = "hireassist"
_lock = threading.Lock()
_configured = False

# keep secrets out of the logs even if a message contains them
_SECRET_PATTERNS = [
    (re.compile(r"(?i)(password|passwd|pwd|secret|token|api[_-]?key|authorization|otp)(\s*[=:]\s*)([^\s,;&'\"}]+)"),
     r"\1\2***"),
]


def get_current_time():
    """Current IST time as 'YYYY-MM-DD HH:MM:SS' (kept for older callers)."""
    now = datetime.now(_IST) if _IST else datetime.now()
    return now.strftime("%Y-%m-%d %H:%M:%S")


def _program_name():
    main_module = sys.modules.get("__main__")
    path = getattr(main_module, "__file__", None) or (sys.argv[0] if sys.argv and sys.argv[0] else "")
    name = os.path.splitext(os.path.basename(path))[0] if path else ""
    if not name or name in ("-c", "-m", "__main__"):
        name = "python"
    return re.sub(r"[^A-Za-z0-9_.-]", "_", name)


class _Formatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        dt = datetime.fromtimestamp(record.created, _IST) if _IST else datetime.fromtimestamp(record.created)
        return dt.strftime("%Y-%m-%d %H:%M:%S") + f".{int(record.msecs):03d}" + (" IST" if _IST else "")

    def format(self, record):
        text = super().format(record)
        for pattern, repl in _SECRET_PATTERNS:
            text = pattern.sub(repl, text)
        return text


_FORMAT = "[%(asctime)s] [%(levelname)s] %(filename)s::%(funcName)s:%(lineno)d - %(message)s"


class _DailyFileHandler(logging.Handler):
    """Appends to <dir>/<prefix>_<YYYY-MM-DD>.log and switches file at midnight (IST).
    Never renames files, so several processes (e.g. the Flask reloader) can share it safely."""

    def __init__(self, directory, prefix, level):
        super().__init__(level)
        self.directory, self.prefix = directory, prefix
        self._day, self._stream = None, None

    def _current_stream(self):
        day = (datetime.now(_IST) if _IST else datetime.now()).strftime("%Y-%m-%d")
        if day != self._day or self._stream is None:
            if self._stream:
                try:
                    self._stream.close()
                except Exception:
                    pass
            path = os.path.join(self.directory, f"{self.prefix}_{day}.log")
            self._stream = open(path, "a", encoding="utf-8")
            self._day = day
        return self._stream

    def emit(self, record):
        try:
            message = self.format(record)
            with _lock:
                stream = self._current_stream()
                stream.write(message + "\n")
                stream.flush()
        except Exception:
            self.handleError(record)

    def close(self):
        if self._stream:
            try:
                self._stream.close()
            except Exception:
                pass
            self._stream = None
        super().close()


def _cleanup_old_logs(directory, days):
    cutoff = time.time() - days * 86400
    try:
        for name in os.listdir(directory):
            path = os.path.join(directory, name)
            if name.endswith(".log") and os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                os.remove(path)
    except Exception:
        pass


def _configure():
    global _configured
    if _configured:
        return logging.getLogger(_LOGGER_NAME)
    with _lock:
        if _configured:
            return logging.getLogger(_LOGGER_NAME)

        # the Windows console (cp1252) cannot print emoji / Indian-language text: replace instead of crashing
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(errors="replace")
            except Exception:
                pass

        level = getattr(logging, str(os.getenv("LOG_LEVEL", "DEBUG")).upper(), logging.DEBUG)
        formatter = _Formatter(_FORMAT)
        handlers = []

        console = logging.StreamHandler(sys.stdout)
        console.setLevel(level)
        console.setFormatter(formatter)
        handlers.append(console)

        if os.getenv("LOG_TO_FILE", "1") != "0":
            log_dir = os.getenv("LOG_DIR") or os.path.join(_PROJECT_DIR, "logs")
            try:
                os.makedirs(log_dir, exist_ok=True)
                program = _program_name()
                for prefix, lvl in ((program, level), (f"{program}_errors", logging.WARNING)):
                    handler = _DailyFileHandler(log_dir, prefix, max(lvl, level))
                    handler.setFormatter(formatter)
                    handlers.append(handler)
                _cleanup_old_logs(log_dir, int(os.getenv("LOG_RETENTION_DAYS", "30")))
            except Exception as e:
                print(f"[logger] File logging disabled: {e}")

        logger = logging.getLogger(_LOGGER_NAME)
        logger.setLevel(level)
        logger.propagate = False
        for h in handlers:
            logger.addHandler(h)

        # other libraries: only their warnings/errors, in the same format and files
        root = logging.getLogger()
        if not root.handlers:
            root.setLevel(logging.WARNING)
            for h in handlers:
                root.addHandler(h)
        # Flask's per-request lines are replaced by the apps' own request logging
        logging.getLogger("werkzeug").setLevel(logging.WARNING)
        # pdfminer (used by pdfplumber) warns once per font quirk; our PDF code logs the outcome itself
        logging.getLogger("pdfminer").setLevel(logging.ERROR)

        # crashes nobody caught still end up in the log
        def _excepthook(exc_type, exc, tb):
            if issubclass(exc_type, KeyboardInterrupt):
                sys.__excepthook__(exc_type, exc, tb)
                return
            logger.critical("Uncaught exception", exc_info=(exc_type, exc, tb))

        def _thread_excepthook(args):
            if args.exc_type is SystemExit:
                return
            # Flask debug reloader on Windows: the old server thread dies with WinError 10038 when the
            # app restarts after a code change. Harmless - the new process serves the requests.
            if (issubclass(args.exc_type, OSError) and getattr(args.exc_value, "winerror", None) == 10038
                    and "serve_forever" in str(getattr(args.thread, "name", ""))):
                logger.debug("Web server thread stopped (app restarting after a code change)")
                return
            logger.critical(f"Uncaught exception in thread {getattr(args.thread, 'name', '?')}",
                            exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

        sys.excepthook = _excepthook
        threading.excepthook = _thread_excepthook
        _configured = True
        return logger


_logger = _configure()


def _in_except_block():
    return sys.exc_info()[0] is not None


def debug_log(message, exc_info=False):
    _logger.debug(message, exc_info=exc_info, stacklevel=2)


def info_log(message, exc_info=False):
    _logger.info(message, exc_info=exc_info, stacklevel=2)


def warning_log(message, exc_info=False):
    _logger.warning(message, exc_info=exc_info, stacklevel=2)


def error_log(message, exc_info=None):
    """Logs an error. Inside an `except` block the traceback is added automatically
    (pass exc_info=False to leave it out)."""
    if exc_info is None:
        exc_info = _in_except_block()
    _logger.error(message, exc_info=exc_info, stacklevel=2)


def exception_log(message):
    """Error with the current exception's traceback (call inside an `except` block)."""
    _logger.error(message, exc_info=True, stacklevel=2)


@contextmanager
def log_duration(action, level="INFO", slow_ms=None):
    """Logs how long a block took:  with log_duration("Gmail send to x@y.com"): ...
    Failures are logged as errors (with traceback) and re-raised.
    slow_ms: log as WARNING when the block takes longer than this."""
    start = time.perf_counter()
    try:
        yield
    except Exception as e:
        elapsed = (time.perf_counter() - start) * 1000
        _logger.error(f"{action} failed after {elapsed:.0f} ms: {e}", exc_info=True, stacklevel=3)
        raise
    elapsed = (time.perf_counter() - start) * 1000
    lvl = logging.WARNING if slow_ms is not None and elapsed > slow_ms else getattr(logging, level.upper(), logging.INFO)
    _logger.log(lvl, f"{action} completed in {elapsed:.0f} ms", stacklevel=3)


def _lan_ip():
    import socket
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))           # no packet is sent; only picks the outgoing interface
            return s.getsockname()[0]
    except Exception:
        return None


def log_app_urls(app_name, port, ssl=True, debug=False):
    """Logs the addresses a web app can be opened at (call just before app.run).
    With debug=True Flask starts a watcher process plus the serving process; only the latter logs."""
    scheme = "https" if ssl else "http"
    lan = _lan_ip()
    serving = (not debug) or os.environ.get("WERKZEUG_RUN_MAIN") == "true"
    if serving:
        bar = "=" * 78
        lines = [f"{app_name} is running on port {port} - open it at:", bar,
                 f"    Local URL   : {scheme}://localhost:{port}/"]
        if lan:
            lines.append(f"    Network URL : {scheme}://{lan}:{port}/   (from other computers)")
        if ssl:
            lines.append("    Use https:// (not http://). The browser warns about the self-signed "
                         "certificate: choose Advanced > Proceed.")
        lines.append(bar)
        _logger.info("\n".join(lines), stacklevel=2)


def short(value, limit=200):
    """Shortened single-line text for logs (CV text, LLM output, email bodies)."""
    text = " ".join(str(value).split()) if value is not None else ""
    return text if len(text) <= limit else text[:limit] + f"... [{len(text)} chars]"


def mask_email(email):
    """'john.doe@example.com' -> 'jo***@example.com' (for logs that may be shared)."""
    email = str(email or "")
    if "@" not in email:
        return email
    user, domain = email.split("@", 1)
    return (user[:2] + "***@" + domain) if user else "***@" + domain
