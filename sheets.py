import os
import re
import json
import gspread
from typing import Dict, Tuple
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
        if pct >= 0:
            targets[asset] = pct
        else:
            print(
                f"⚠️  Warning: Invalid target percentage '{pct_str}' for asset '{asset}' - defaulting to 0%"
            )
            targets[asset] = 0.0

    total = sum(targets.values())
    if abs(total - 1.0) > 0.02:
        print(f"⚠️  Warning: Target percentages sum to {total:.2f}, not 1.0")

    return targets


def _parse_sheet_numeric_value(val) -> float:
    """Parse a numeric value from a sheet cell, stripping currency formatting."""
    if val is None or val == "":
        return 0.0
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, str):
        cleaned = val.strip().replace("$", "").replace(",", "")
        try:
            return float(cleaned)
        except ValueError:
            raise ValueError(f"Expected a numeric reserve value in I3, got: {val!r}")
    raise ValueError(f"Unsupported reserve cell type: {type(val).__name__}")


def get_desired_cash_reserve() -> float:
    """Return the desired cash reserve at cell I3 from the 'Signals' sheet.

    The sheet cell I3 is expected to contain a numeric or currency-formatted value
    like '$860' or '860'.
    """
    spreadsheet_id = os.getenv("GOOGLE_DOCS_SHEET_ID")
    if not spreadsheet_id:
        raise ValueError("GOOGLE_DOCS_SHEET_ID not set in .env")

    gc = get_gspread_client()
    worksheet = gc.open_by_key(spreadsheet_id).worksheet("Signals")
    value = worksheet.acell("I3").value
    return _parse_sheet_numeric_value(value)


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

    # ({'SOL': 190.8, 'BTC': 1234.56, ...}, total_value)
    portfolio, total_value, stable_breakdown = fetch_portfolio()

    gc = get_gspread_client()
    worksheet = gc.open_by_key(spreadsheet_id).worksheet("Signals")

    # Read only Column A (assets)
    asset_rows = worksheet.get_values("A8:A")

    # Collect values for Column E (one contiguous range update)
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
    else:
        results.append("⚠️ No assets found in the Signals sheet (starting at row 8).")

    # Update stable asset values
    stable_rows = worksheet.get_values("H8:H")
    current_stable_values = []

    for row in stable_rows:
        if not row or not row[0]:
            break  # stop at first empty asset row

        asset = str(row[0]).strip().upper()
        usd_value = stable_breakdown.get(asset, 0.0)  # 0.0 if we hold nothing
        current_stable_values.append(
            [usd_value]
        )  # list-of-lists for gspread column update

    if current_stable_values:
        # Update the entire "Value" column in one batch
        last_row = 7 + len(current_stable_values)
        worksheet.update(
            f"I8:I{last_row}",
            current_stable_values,
            value_input_option="RAW",  # keeps the value as a real number (not text)
        )
        results.append(
            f"✅ Updated values for {len(current_stable_values)} stable assets."
        )
    else:
        results.append(
            "⚠️ No stable assets found in the Signals sheet (starting at row 8)."
        )

    if current_usd_values and current_stable_values:
        results.append(f"💰 Total portfolio value: ${total_value:,.2f}")

    return {"results": results}


def parse_signal_update(text: str) -> Dict[str, Tuple[float, str]]:
    """Parse the exact Telegram 'Portfolio Signal Update' format.
    Returns {ASSET: (target_pct_decimal, direction)} e.g. {'HYPE': (0.344, 'Long')}
    CASH lines are completely skipped.
    """
    # Matches lines like: - 34.4% HYPE LONG 🟢   or   - 17.3% CASH 💵
    pattern = r"-\s+([\d.]+)%\s+([A-Z0-9]+)(?:\s+(LONG|CASH))?"
    matches = re.findall(pattern, text, re.IGNORECASE)
    targets: Dict[str, Tuple[float, str]] = {}
    for pct_str, asset, direction in matches:
        asset = asset.strip().upper()
        if asset == "CASH":
            continue  # Skip CASH completely
        pct = float(pct_str) / 100
        dir_val = (direction or "Long").capitalize()  # ← now "Long" (not LONG)
        targets[asset] = (pct, dir_val)
    return targets


def update_targets_from_signal(signal_text: str) -> dict:
    """Parse signal and update (or append) target % in Google Sheet 'Signals' worksheet.
    - Updates existing assets (Columns B+C).
    - For NEW assets: full copyPaste of the row above (A:F) → preserves formulas in D & F,
      ALL formatting, colors, borders, conditional formatting, etc.
      Then overwrites only A:C with the new data.
    - Column B = "Long" (capitalized as requested).
    - Uses USER_ENTERED everywhere → no more leading ' backtick.
    """
    targets = parse_signal_update(signal_text)
    if not targets:
        return {"status": "error", "message": "No valid targets found in signal"}

    spreadsheet_id = os.getenv("GOOGLE_DOCS_SHEET_ID")
    if not spreadsheet_id:
        raise ValueError("GOOGLE_DOCS_SHEET_ID not set in .env")

    gc = get_gspread_client()
    worksheet = gc.open_by_key(spreadsheet_id).worksheet("Signals")

    # Read current assets (Column A)
    asset_rows = worksheet.get_values("A8:A")
    existing = {}  # asset -> row_index_1based
    for i, row in enumerate(asset_rows, start=8):
        if not row or not row[0]:
            break
        asset = str(row[0]).strip().upper()
        existing[asset] = i

    # Prepare updates
    updates = []
    new_rows_data = []
    for asset, (pct, direction) in targets.items():
        pct_str = f"{pct * 100:.1f}%"
        if asset in existing:
            row = existing[asset]
            updates.append(
                {"range": f"B{row}:C{row}", "values": [[direction, pct_str]]}
            )
        else:
            new_rows_data.append([asset, direction, pct_str])

    results = []
    if updates:
        worksheet.batch_update(updates, value_input_option="USER_ENTERED")
        results.append(f"✅ Updated {len(updates)} existing assets")

    if new_rows_data:
        next_row = 8 + len(existing)
        last_existing_row = next_row - 1

        for i, new_asset_data in enumerate(new_rows_data):
            current_new_row = next_row + i

            # === FULL ROW COPY (A:F) preserves formulas + formatting ===
            if last_existing_row >= 8:
                copy_request = {
                    "copyPaste": {
                        "source": {
                            "sheetId": worksheet.id,
                            "startRowIndex": last_existing_row - 1,
                            "endRowIndex": last_existing_row,
                            "startColumnIndex": 0,
                            "endColumnIndex": 6,
                        },
                        "destination": {
                            "sheetId": worksheet.id,
                            "startRowIndex": current_new_row - 1,
                            "endRowIndex": current_new_row,
                            "startColumnIndex": 0,
                            "endColumnIndex": 6,
                        },
                        "pasteType": "PASTE_NORMAL",  # ← FIXED: this was the crash
                        "pasteOrientation": "NORMAL",
                    }
                }
                # Use spreadsheet.batch_update for structural requests like copyPaste
                worksheet.spreadsheet.batch_update({"requests": [copy_request]})

            # === Overwrite only A:C with new clean data ===
            asset_name, direction, pct_str = new_asset_data
            worksheet.update(
                f"A{current_new_row}:C{current_new_row}",
                [[asset_name, direction, pct_str]],
                value_input_option="USER_ENTERED",
            )

        results.append(
            f"✅ Added {len(new_rows_data)} new asset(s) with full row copy (A:F)"
        )

    total = sum(pct for pct, _ in targets.values())
    results.append(f"📊 New targets sum: {total:.1%} ({len(targets)} assets)")

    return {"status": "success", "message": " | ".join(results), "targets": targets}
