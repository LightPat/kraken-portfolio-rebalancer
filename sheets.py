import os
import json
import gspread
from kraken import fetch_portfolio
from oauth2client.service_account import ServiceAccountCredentials

# Global cached client
_gspread_client = None


def get_gspread_client():
    """Get authenticated gspread client (cached for performance)."""
    global _gspread_client
    if _gspread_client is not None:
        return _gspread_client

    # Preferred: JSON file (Linux droplet)
    creds_path = os.getenv("GOOGLE_CREDENTIALS")
    if creds_path and os.path.exists(creds_path):
        scope = [
            "https://spreadsheets.google.com/feeds",
            "https://www.googleapis.com/auth/drive",
        ]
        credentials = ServiceAccountCredentials.from_json_keyfile_name(
            creds_path, scope
        )
        _gspread_client = gspread.authorize(credentials)
        return _gspread_client

    # Fallback: JSON string (local dev with Dashlane)
    creds_json = os.getenv("GOOGLE_CREDENTIALS")
    if not creds_json:
        raise ValueError(
            "Neither GOOGLE_CREDENTIALS (filepath) nor (JSON string) "
            "found in .env file. Check your environment setup."
        )
    creds_dict = json.loads(creds_json)
    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
    ]
    credentials = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    _gspread_client = gspread.authorize(credentials)
    return _gspread_client


def _parse_target_percentage(val) -> float:
    """Parse a percentage value like '0.0%', '40%', '0.4' or 25 into a float fraction (0.0-1.0)."""
    if val is None or val == "":
        return 0.0
    if isinstance(val, (int, float)):
        val = float(val)
        return val / 100 if val > 1 else val
    if isinstance(val, str):
        val = val.strip().rstrip("%").strip()
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


def update_current_allocations_in_sheet():
    """Update the 'Current' column (Column E) in the Google Sheet with current USD values.

    For each asset listed in Column A (starting at row 8), this puts the total USD value
    of your holdings (quantity x current price) into Column E.

    Example:
      - You hold 2 SOL and SOL price = $95.40 → cell gets 190.8 (raw number)
      - Sheet can be formatted as Currency / Number with 2 decimals if desired.

    Stops at the first empty row in Column A (same logic as get_target_allocations).
    Uses the exact portfolio values returned by fetch_portfolio() (no extra API calls).
    """
    spreadsheet_id = os.getenv("GOOGLE_DOCS_SHEET_ID")
    if not spreadsheet_id:
        raise ValueError("GOOGLE_DOCS_SHEET_ID not set in .env")

    # fetch_portfolio returns exactly what you asked for:
    # ({'SOL': 190.8, 'BTC': 1234.56, ...}, total_value)
    portfolio, total_value = fetch_portfolio()

    gc = get_gspread_client()
    worksheet = gc.open_by_key(spreadsheet_id).worksheet("Signals")

    # Read only Column A (assets) – one lightweight API call
    asset_rows = worksheet.get_values("A8:A")

    # Collect values for Column D (one contiguous range update)
    current_usd_values = []
    for row in asset_rows:
        if not row or not row[0]:
            break  # stop at first empty asset row (matches get_target_allocations)

        asset = str(row[0]).strip().upper()
        usd_value = portfolio.get(asset, 0.0)  # 0.0 if we hold nothing
        current_usd_values.append(
            [usd_value]
        )  # list-of-lists for gspread column update

    results = []

    if current_usd_values:
        # Update the entire "Current" column in one batch
        last_row = 7 + len(current_usd_values)
        worksheet.update(
            f"E8:E{last_row}",
            current_usd_values,
            value_input_option="RAW",  # keeps the value as a real number (not text)
        )
        results.append(
            f"✅ Updated current USD values for {len(current_usd_values)} assets."
        )
        results.append(f"💰 Total portfolio value: ${total_value:,.2f}")
    else:
        results.append("⚠️ No assets found in the Signals sheet (starting at row 8).")

    return {"results": results}
