import pandas as pd

def read_excel_file(file_path):
    return pd.read_excel(file_path)

def map_excel_to_cv(row):
    return {
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