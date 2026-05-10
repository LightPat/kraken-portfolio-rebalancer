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


def _parse_target_percentage(val) -> float:
    """Parse a percentage value like '0.0%', '40%', '0.4' or 25 into a float fraction (0.0-1.0)."""
    if val is None or val == '':
        return 0.0
    if isinstance(val, (int, float)):
        val = float(val)
        return val / 100 if val > 1 else val
    if isinstance(val, str):
        val = val.strip().rstrip('%').strip()
        try:
            pct = float(val)
            return pct / 100 if pct > 1 else pct
        except ValueError:
            return 0.0
    return 0.0


def get_target_allocations() -> dict[str, float]:
    """Return {ASSET: target_percentage} from Google Sheet.

    Expects rows like ['BTC', 'Long', '0.0%'] or ['ETH', 'Long', '40%'] starting from row 8 in "Signals" sheet.
    - Column A: Asset
    - Column B: Direction (e.g. Long) - ignored for now
    - Column C: Target percentage string like '0.0%' or '40%'
    Stops at the first empty row in Column A.
    Percentages converted to decimal (0.0 - 1.0).
    """
    spreadsheet_id = os.getenv("GOOGLE_DOCS_SHEET_ID")
    if not spreadsheet_id:
        raise ValueError("GOOGLE_DOCS_SHEET_ID not set in .env")

    gc = get_gspread_client()
    worksheet = gc.open_by_key(spreadsheet_id).worksheet("Signals")

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

        # Parse percentage from column C, handling 'XX%' format
        pct_str = row[2] if len(row) > 2 else None
        pct = _parse_target_percentage(pct_str)
        if pct > 0:
            targets[asset] = pct

    total = sum(targets.values())
    if abs(total - 1.0) > 0.02:
        print(f"⚠️  Warning: Target percentages sum to {total:.2f}, not 1.0")

    return targets
