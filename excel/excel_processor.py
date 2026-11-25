import os

import pandas as pd

from utils.logger import debug_log, info_log, log_duration


def read_excel_file(file_path):
    debug_log(f"Reading Excel file: {os.path.basename(str(file_path))}")
    with log_duration(f"Excel read of {os.path.basename(str(file_path))}", level="DEBUG"):
        df = pd.read_excel(file_path)
    info_log(f"Excel file {os.path.basename(str(file_path))} loaded - {len(df)} rows, {len(df.columns)} columns")
    return df


def map_excel_to_cv(row):
    cv = {
        "name": row["Name"],
        "email": row["Email"],
        "phone": row["Contact Details"],
        "location": row["Current Location"],
        "skills": row["Key Skills"],
        "experience": row["Total Experience"],
        "summary": row["Summary"],
        "headline": row["Resume Headline"],
        "job_id": row["Job ID"]
    }
    debug_log(f"Excel row mapped to CV - Job ID: {cv['job_id']}, has email: {bool(cv['email'])}")
    return cv
