import os
import json
import gspread
from oauth2client.service_account import ServiceAccountCredentials


def get_gspread_client():
    creds_json = os.getenv("GOOGLE_CREDENTIALS")
    if not creds_json:
        raise ValueError("GOOGLE_CREDENTIALS not found in .env")
    creds_dict = json.loads(creds_json)
    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
    ]
    credentials = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    return gspread.authorize(credentials)


def get_target_allocations() -> dict[str, float]:
    """Return {ASSET: target_percentage} from Google Sheet"""
    spreadsheet_id = os.getenv("SPREADSHEET_ID")
    if not spreadsheet_id:
        raise ValueError("SPREADSHEET_ID not set in .env")

    gc = get_gspread_client()
    worksheet = gc.open_by_key(spreadsheet_id).worksheet("Portfolio")
    data = worksheet.get_all_records()

    targets = {}
    for row in data:
        asset = str(row.get("Asset", "")).strip().upper()
        if not asset:
            continue
        try:
            pct = float(row.get("Target Percentage", 0))
            if pct > 0:
                targets[asset] = pct
        except ValueError, TypeError:
            continue

    total = sum(targets.values())
    if abs(total - 1.0) > 0.02:
        print(f"⚠️  Warning: Target percentages sum to {total:.2f}, not 1.0")

    return targets
