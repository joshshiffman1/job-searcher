#!/usr/bin/env python3
"""
update_sheets.py — Sync job search results to Google Sheets.

Reads the latest analysis JSON from .temp or daily-reports and appends
new job results to a Google Sheet, deduplicating by URL.

Usage:
    python scripts/update_sheets.py --analysis PATH --sheet-id SHEET_ID
"""

import argparse
import json
import os
import sys
from datetime import date
from pathlib import Path

import google.auth
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
SHEET_NAME = "Jobs"
HEADERS = [
    "Date Found",
    "Score",
    "New?",
    "Title",
    "Company",
    "Location",
    "Salary",
    "Link",
    "Key Qualifications",
    "Reasoning",
]


def get_sheets_service():
    """Build Google Sheets service from env var credentials."""
    creds_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not creds_json:
        print("[SHEETS] ERROR: GOOGLE_SERVICE_ACCOUNT_JSON not set", file=sys.stderr)
        sys.exit(1)

    try:
        creds_dict = json.loads(creds_json)
    except json.JSONDecodeError as e:
        print(f"[SHEETS] ERROR: Invalid JSON in GOOGLE_SERVICE_ACCOUNT_JSON: {e}", file=sys.stderr)
        sys.exit(1)

    creds = service_account.Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    return build("sheets", "v4", credentials=creds)


def ensure_sheet_exists(service, sheet_id):
    """Make sure the Jobs sheet tab exists with headers."""
    try:
        spreadsheet = service.spreadsheets().get(spreadsheetId=sheet_id).execute()
        sheet_names = [s["properties"]["title"] for s in spreadsheet["sheets"]]

        if SHEET_NAME not in sheet_names:
            # Create the sheet tab
            service.spreadsheets().batchUpdate(
                spreadsheetId=sheet_id,
                body={
                    "requests": [{
                        "addSheet": {
                            "properties": {"title": SHEET_NAME}
                        }
                    }]
                }
            ).execute()
            print(f"[SHEETS] Created '{SHEET_NAME}' tab", file=sys.stderr)

            # Add headers
            service.spreadsheets().values().update(
                spreadsheetId=sheet_id,
                range=f"{SHEET_NAME}!A1",
                valueInputOption="RAW",
                body={"values": [HEADERS]}
            ).execute()
            print("[SHEETS] Headers written", file=sys.stderr)
            return []

        # Get existing URLs to deduplicate
        result = service.spreadsheets().values().get(
            spreadsheetId=sheet_id,
            range=f"{SHEET_NAME}!H:H"  # Link column
        ).execute()
        existing_urls = set()
        for row in result.get("values", [])[1:]:  # Skip header
            if row:
                existing_urls.add(row[0].strip())
        return existing_urls

    except HttpError as e:
        print(f"[SHEETS] ERROR: {e}", file=sys.stderr)
        sys.exit(1)


def load_analysis(analysis_path):
    """Load the Claude analysis JSON."""
    p = Path(analysis_path)
    if not p.exists():
        print(f"[SHEETS] ERROR: Analysis file not found: {analysis_path}", file=sys.stderr)
        sys.exit(1)

    with open(p) as f:
        return json.load(f)


def format_row(job, run_date):
    """Format a job dict into a sheet row."""
    qualifications = "; ".join(job.get("qualifications") or [])
    is_new = "★ NEW" if job.get("is_new") else ""
    salary = job.get("salary") or ""
    if salary == "null":
        salary = ""

    return [
        run_date,
        job.get("score", ""),
        is_new,
        job.get("title", ""),
        job.get("company", ""),
        job.get("location", ""),
        salary,
        job.get("link", ""),
        qualifications,
        job.get("reasoning", ""),
    ]


def append_jobs(service, sheet_id, jobs, existing_urls, run_date):
    """Append new jobs to the sheet."""
    rows_to_add = []
    skipped = 0

    # Sort by score descending
    sorted_jobs = sorted(jobs, key=lambda j: j.get("score", 0), reverse=True)

    for job in sorted_jobs:
        url = job.get("link", "").strip()
        if url in existing_urls:
            skipped += 1
            continue
        rows_to_add.append(format_row(job, run_date))
        existing_urls.add(url)

    if not rows_to_add:
        print(f"[SHEETS] No new jobs to add ({skipped} already in sheet)", file=sys.stderr)
        return 0

    try:
        service.spreadsheets().values().append(
            spreadsheetId=sheet_id,
            range=f"{SHEET_NAME}!A1",
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": rows_to_add}
        ).execute()
        print(f"[SHEETS] Added {len(rows_to_add)} new job(s) ({skipped} skipped as duplicates)", file=sys.stderr)
        return len(rows_to_add)

    except HttpError as e:
        print(f"[SHEETS] ERROR appending rows: {e}", file=sys.stderr)
        return 0


def main():
    parser = argparse.ArgumentParser(description="Sync job results to Google Sheets")
    parser.add_argument("--analysis", required=True, help="Path to Claude analysis JSON file")
    parser.add_argument("--sheet-id", required=True, help="Google Sheet ID")
    args = parser.parse_args()

    print("[SHEETS] Starting Google Sheets sync...", file=sys.stderr)

    service = get_sheets_service()
    existing_urls = ensure_sheet_exists(service, args.sheet_id)
    analysis = load_analysis(args.analysis)

    jobs = analysis.get("jobs", [])
    total = analysis.get("summary", {}).get("total", 0)

    if total == 0 or not jobs:
        print("[SHEETS] No jobs to sync", file=sys.stderr)
        return

    run_date = date.today().isoformat()
    added = append_jobs(service, args.sheet_id, jobs, existing_urls, run_date)

    print(f"[SHEETS] Sync complete — {added} job(s) added to sheet", file=sys.stderr)


if __name__ == "__main__":
    main()
