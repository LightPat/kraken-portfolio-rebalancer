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
    """Return {ASSET: target_percentage} from Google Sheet.

    Reads asset names from Column A (starting at row 8)
    and target percentages from Column C (starting at row 8).
    Stops at the first empty row in Column A.
    """
    spreadsheet_id = os.getenv("GOOGLE_DOCS_SHEET_ID")
    if not spreadsheet_id:
        raise ValueError("GOOGLE_DOCS_SHEET_ID not set in .env")

    gc = get_gspread_client()
    worksheet = gc.open_by_key(spreadsheet_id).worksheet("Portfolio")

    # Fetch only the needed columns starting at row 8 (one API call)
    values = worksheet.get_values("A8:C")

    targets = {}
    for row in values:
        # row[0] = Column A (Asset), row[2] = Column C (Percentage)
        if not row or len(row) == 0:
            break

        asset = str(row[0] if len(row) > 0 else "").strip().upper()
        if not asset:
            break  # Stop at the first empty asset row (as requested)

        try:
            pct = float(row[2] if len(row) > 2 else 0)
            if pct > 0:
                targets[asset] = pct
        except ValueError, TypeError:
            continue

    total = sum(targets.values())
    if abs(total - 1.0) > 0.02:
        print(f"⚠️  Warning: Target percentages sum to {total:.2f}, not 1.0")

    return targets
