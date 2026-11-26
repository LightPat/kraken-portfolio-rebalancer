import os
import re
import json
import math
import logging
import time
from threading import Lock
import httpx
import gspread
from typing import Any, Dict, Tuple, TypedDict
from kraken import (
    fetch_portfolio,
    get_btc_usd_price,
    get_kraken_btc_balance,
)
from oauth2client.service_account import ServiceAccountCredentials

# Global cached client
_gspread_client = None
logger = logging.getLogger(__name__)


class ColdBalanceCache(TypedDict):
    address: str
    timestamp: float
    balance: float


_BTC_COLD_BALANCE_CACHE: ColdBalanceCache | None = None
_BTC_COLD_BALANCE_CACHE_LOCK = Lock()
# Cache within a rebalance cycle; BTC only moves between Kraken and this Trezor.
BTC_COLD_BALANCE_CACHE_TTL_SECONDS = 90


class TargetAllocation(TypedDict):
    pct: float
    direction: str


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
        s = val.strip()
        has_percent = s.endswith("%")
        s = s.rstrip("%").strip()
        try:
            pct = float(s)
            return pct / 100 if has_percent or pct > 1 else pct
        except ValueError:
            return 0.0
    return 0.0


def get_target_allocations() -> dict[str, TargetAllocation]:
    """Return {ASSET: {pct, direction}} from Google Sheet.

    Expects rows like ['BTC', 'Long', '0.0%'] or ['ETH', 'Short', '25%']
    starting from row 8 in "Signals" sheet.
    - Column A: Asset
    - Column B: Direction (Long or Short)
    - Column C: Positive target percentage
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

    targets: dict[str, TargetAllocation] = {}
    for row_number, row in enumerate(values, start=8):
        if not row or len(row) == 0:
            break

        asset = str(row[0] if len(row) > 0 else "").strip().upper()
        if not asset:
            break  # Stop at the first empty asset row (as requested)

        direction_raw = str(row[1] if len(row) > 1 else "").strip().lower()
        direction = "Short" if direction_raw == "short" else "Long"

        pct_str = row[2] if len(row) > 2 else None
        pct = _parse_target_percentage(pct_str)
        if pct < 0:
            print(
                f"⚠️  Warning: Negative target percentage '{pct_str}' for "
                f"asset '{asset}' in row {row_number} - setting to 0%"
            )
            pct = 0.0
        targets[asset] = {"pct": pct, "direction": direction}

    total = sum(target["pct"] for target in targets.values())
    if abs(total - 1.0) > 0.02:
        print(f"⚠️  Warning: Long + Short capital allocations sum to {total:.2f}, not 1.0")

    return targets


def _parse_sheet_numeric_value(val, cell: str = "I3") -> float:
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
            raise ValueError(f"Expected a numeric value in {cell}, got: {val!r}")
    raise ValueError(f"Unsupported value in {cell}: {type(val).__name__}")


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


def _get_signals_worksheet():
    spreadsheet_id = os.getenv("GOOGLE_DOCS_SHEET_ID")
    if not spreadsheet_id:
        raise ValueError("GOOGLE_DOCS_SHEET_ID not set in .env")

    return get_gspread_client().open_by_key(spreadsheet_id).worksheet("Signals")


def _parse_fraction_cell(value, cell: str) -> float:
    if value is None or value == "":
        raise ValueError(f"{cell} is empty")
    if isinstance(value, str) and not value.strip().endswith("%"):
        try:
            parsed = float(value.strip())
        except ValueError as exc:
            raise ValueError(f"Expected a percentage in {cell}, got: {value!r}") from exc
        if not 0 <= parsed <= 1:
            raise ValueError(f"Expected {cell} between 0 and 1, got: {value!r}")
        return parsed
    parsed = _parse_target_percentage(value)
    if not 0 <= parsed <= 1:
        raise ValueError(f"Expected {cell} between 0 and 1, got: {value!r}")
    return parsed


def get_rsps_allocation_pct() -> float:
    """Return the RSPS share from Signals!N2 as a decimal fraction."""
    value = _get_signals_worksheet().acell("N2").value
    return _parse_fraction_cell(value, "N2")


def get_sdca_cash_on_kraken() -> float:
    """Return the manually tracked SDCA cash held on Kraken from Signals!Q6."""
    cash_on_kraken = _parse_sheet_numeric_value(
        _get_signals_worksheet().acell("Q6").value, "Q6"
    )
    if not math.isfinite(cash_on_kraken) or cash_on_kraken < 0:
        raise ValueError("SDCA cash on Kraken in Q6 must be finite and non-negative")
    return cash_on_kraken


def get_sdca_sheet_values() -> dict[str, float]:
    """Read SDCA's cash, signed daily change fraction, and tracked BTC balances."""
    worksheet = _get_signals_worksheet()
    cash = _parse_sheet_numeric_value(worksheet.acell("Q3").value, "Q3")
    change_pct_value = worksheet.acell("Q4").value
    change_pct_text = str(change_pct_value).strip()
    has_percent_sign = change_pct_text.endswith("%")
    try:
        change_pct = float(
            change_pct_text[:-1].strip() if has_percent_sign else change_pct_text
        )
        if has_percent_sign:
            change_pct /= 100
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"Expected a signed decimal change or percentage in Q4, got: {change_pct_value!r}"
        ) from exc
    if not math.isfinite(change_pct):
        raise ValueError(f"Expected a finite signed decimal change in Q4")
    kraken_btc = _parse_sheet_numeric_value(worksheet.acell("Q7").value, "Q7")
    cold_btc = _parse_sheet_numeric_value(worksheet.acell("Q8").value, "Q8")
    if kraken_btc < 0 or cold_btc < 0:
        raise ValueError("SDCA BTC balances must not be negative")
    return {
        "cash_usd": cash,
        "change_pct": change_pct,
        "kraken_btc": kraken_btc,
        "cold_btc": cold_btc,
    }


def parse_sdca_signal_update(signal_text: str) -> float:
    """Parse an SDCA signal into a signed decimal change for Signals!Q4."""
    default_match = re.search(
        r"threshold\s+not\s+passed.*?default\s+to\s+~?\s*"
        r"([+-]?\d+(?:\.\d+)?)\s*%",
        signal_text,
        re.IGNORECASE | re.DOTALL,
    )
    if default_match:
        return abs(float(default_match.group(1))) / 100

    action_matches = []
    for action, pattern in (
        (
            "buy",
            r"\bBUY\s+([+-]?\d+(?:\.\d+)?)\s*%",
        ),
        (
            "sell",
            r"\b(?:SELL|DISTRIBUTE)\s+([+-]?\d+(?:\.\d+)?)\s*%",
        ),
    ):
        for match in re.finditer(pattern, signal_text, re.IGNORECASE):
            percentage = float(match.group(1))
            if action == "buy" and percentage < 0:
                raise ValueError("An SDCA BUY percentage cannot be negative")
            signed_pct = abs(percentage) if action == "buy" else -abs(percentage)
            action_matches.append(signed_pct / 100)

    negative_matches = [
        -float(match.group(1)) / 100
        for match in re.finditer(
            r"(?<![\w.])-\s*(\d+(?:\.\d+)?)\s*%", signal_text
        )
    ]
    parsed_values = set(action_matches + negative_matches)
    if len(parsed_values) == 1:
        return parsed_values.pop()
    if len(parsed_values) > 1:
        raise ValueError("SDCA signal contains conflicting change percentages")
    raise ValueError("No SDCA change percentage could be extracted")


def update_sdca_change_in_sheet(change_pct: float) -> None:
    """Write only the signed SDCA change fraction to Signals!Q4."""
    if not math.isfinite(change_pct):
        raise ValueError("SDCA change percentage must be a finite number")
    _get_signals_worksheet().update(
        "Q4", [[change_pct]], value_input_option="RAW"
    )


def update_sdca_cash_after_fill(
    action: str, actual_quote_amount_usd: float
) -> dict[str, float]:
    """Apply a filled SDCA trade to Values To Edit!B4 and B5."""
    if action not in {"buy", "sell"}:
        raise ValueError(f"Unsupported SDCA cash action: {action!r}")

    amount = float(actual_quote_amount_usd)
    if not math.isfinite(amount) or amount < 0:
        raise ValueError("SDCA fill amount must be finite and non-negative")

    spreadsheet_id = os.getenv("GOOGLE_DOCS_SHEET_ID")
    if not spreadsheet_id:
        raise ValueError("GOOGLE_DOCS_SHEET_ID not set in .env")

    worksheet = get_gspread_client().open_by_key(spreadsheet_id).worksheet(
        "Values To Edit"
    )
    usd_left = _parse_sheet_numeric_value(
        worksheet.acell("B4").value, "Values To Edit!B4"
    )
    cash_on_kraken = _parse_sheet_numeric_value(
        worksheet.acell("B5").value, "Values To Edit!B5"
    )
    delta = amount if action == "sell" else -amount
    updated = {
        "usd_left": max(0.0, usd_left + delta),
        "cash_on_kraken": max(0.0, cash_on_kraken + delta),
    }

    worksheet.batch_update(
        [
            {"range": "B4", "values": [[updated["usd_left"]]]},
            {"range": "B5", "values": [[updated["cash_on_kraken"]]]},
        ],
        value_input_option="RAW",
    )
    return updated


def _fetch_mempool_address(address: str) -> dict[str, Any]:
    response = httpx.get(
        f"https://mempool.space/api/address/{address}", timeout=10.0
    )
    response.raise_for_status()
    data = response.json()
    for stats_key in ("chain_stats", "mempool_stats"):
        stats = data.get(stats_key)
        if not isinstance(stats, dict) or not {
            "funded_txo_sum",
            "spent_txo_sum",
            "tx_count",
        }.issubset(stats):
            raise ValueError(
                f"Blockchain API response is missing {stats_key} for {address}"
            )
    return data


def clear_btc_cold_balance_cache() -> None:
    """Invalidate the cached public balance after an SDCA trade."""
    global _BTC_COLD_BALANCE_CACHE
    with _BTC_COLD_BALANCE_CACHE_LOCK:
        _BTC_COLD_BALANCE_CACHE = None


def get_btc_cold_balance(force_refresh: bool = False) -> float:
    """Return the confirmed and unconfirmed balance of the configured Trezor address."""
    global _BTC_COLD_BALANCE_CACHE
    address = os.getenv("TREZOR_BTC_ADDRESS", "").strip()
    if not address:
        return 0.0

    with _BTC_COLD_BALANCE_CACHE_LOCK:
        now = time.monotonic()
        cached = (
            _BTC_COLD_BALANCE_CACHE
            if _BTC_COLD_BALANCE_CACHE
            and _BTC_COLD_BALANCE_CACHE["address"] == address
            else None
        )
        if (
            not force_refresh
            and cached
            and now - cached["timestamp"] <= BTC_COLD_BALANCE_CACHE_TTL_SECONDS
        ):
            print("🧊 Using cached Trezor BTC balance.")
            return cached["balance"]

        try:
            data = _fetch_mempool_address(address)
            balance_satoshis = sum(
                int(data[stats_key]["funded_txo_sum"])
                - int(data[stats_key]["spent_txo_sum"])
                for stats_key in ("chain_stats", "mempool_stats")
            )
            balance = balance_satoshis / 100_000_000
        except Exception as exc:
            logger.warning(
                "Trezor BTC address lookup failed; using cached balance or zero: %s",
                exc,
            )
            return cached["balance"] if cached else 0.0

        _BTC_COLD_BALANCE_CACHE = {
            "address": address,
            "timestamp": time.monotonic(),
            "balance": balance,
        }
        return balance


def update_current_allocations_in_sheet(
    sdca_btc_adjustment: float = 0.0,
    force_cold_balance_refresh: bool = False,
):
    """Update Column E with current allocation capital in USD.

    Spot holdings and short used margin are both positive.

    Example:
      - You hold 2 SOL and SOL price = $95.40 → cell gets 190.8 (raw number)
      - A $15,000 short using $7,500 margin → cell gets 7500
      - Sheet can be formatted as Currency / Number with 2 decimals if desired.

    Stops at the first empty row in Column A (same logic as get_target_allocations).
    Uses the portfolio values returned by fetch_portfolio().
    """
    spreadsheet_id = os.getenv("GOOGLE_DOCS_SHEET_ID")
    if not spreadsheet_id:
        raise ValueError("GOOGLE_DOCS_SHEET_ID not set in .env")

    (
        spot_value_by_asset,
        _,
        short_used_margin_by_asset,
        _,
        total_value,
        stable_breakdown,
    ) = fetch_portfolio()
    sdca_values = get_sdca_sheet_values()
    cold_btc = get_btc_cold_balance(force_refresh=force_cold_balance_refresh)
    btc_price = get_btc_usd_price()
    sdca_total_btc = sdca_values["kraken_btc"] + sdca_values["cold_btc"]
    tracked_sdca_total = max(0.0, sdca_total_btc + sdca_btc_adjustment)
    sdca_kraken_btc = round(max(0.0, tracked_sdca_total - cold_btc), 8)
    kraken_btc = get_kraken_btc_balance()
    sdca_kraken_btc = min(sdca_kraken_btc, kraken_btc)
    rsps_btc = round(kraken_btc - sdca_kraken_btc, 8)
    sdca_equity = round(
        sdca_values["cash_usd"] + (sdca_kraken_btc + cold_btc) * btc_price, 2
    )

    current_values = dict(spot_value_by_asset)
    for asset, used_margin in short_used_margin_by_asset.items():
        current_values[asset] = current_values.get(asset, 0.0) + used_margin
    current_values["BTC"] = (
        rsps_btc * btc_price + short_used_margin_by_asset.get("BTC", 0.0)
    )

    gc = get_gspread_client()
    worksheet = gc.open_by_key(spreadsheet_id).worksheet("Signals")
    worksheet.batch_update(
        [
            {"range": "Q7", "values": [[sdca_kraken_btc]]},
            {"range": "Q8", "values": [[cold_btc]]},
            {"range": "Q10", "values": [[sdca_equity]]},
        ],
        value_input_option="RAW",
    )
    worksheet.update("E5", [[rsps_btc]], value_input_option="RAW")

    # Read only Column A (assets)
    asset_rows = worksheet.get_values("A8:A")

    # Collect values for Column E (one contiguous range update)
    current_usd_values = []
    for row in asset_rows:
        if not row or not row[0]:
            break  # stop at first empty asset row (matches get_target_allocations)

        asset = str(row[0]).strip().upper()
        usd_value = current_values.get(asset, 0.0)  # 0.0 if we hold nothing
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
    """Parse RSPS and Short Module targets from a Portfolio Signal Update.

    Returns {ASSET: (target_pct_decimal, direction)}. Net Positions are
    intentionally ignored because they repeat the allocations in the modules.
    """
    section_pattern = re.compile(
        r"(?im)^[^\r\n]*\b(RSPS Signal|Short Module|Net Positions):[^\r\n]*$"
    )
    sections = list(section_pattern.finditer(text))
    if sections:
        signal_sections = []
        for index, section in enumerate(sections):
            if section.group(1).lower() not in {"rsps signal", "short module"}:
                continue
            section_end = (
                sections[index + 1].start()
                if index + 1 < len(sections)
                else len(text)
            )
            signal_sections.append(text[section.end() : section_end])
        signal_text = "\n".join(signal_sections)
    else:
        # Preserve support for older messages that omitted section headings.
        signal_text = text

    pattern = re.compile(
        r"-\s*([\d.]+)%\s+([A-Z0-9]+)(?:\s+(LONG|SHORT|CASH))?\b",
        re.IGNORECASE,
    )
    targets: Dict[str, Tuple[float, str]] = {}
    for line in signal_text.splitlines():
        line = line.replace("*", "")
        match = pattern.search(line)
        if not match:
            continue
        pct_str, asset, direction = match.groups()
        asset = asset.strip().upper()
        if asset == "CASH":
            continue
        targets[asset] = (
            float(pct_str) / 100,
            (direction or "Long").capitalize(),
        )
    return targets


def _direction_format_request(sheet_id: int, row: int, direction: str) -> dict:
    source_row = 1 if direction == "Long" else 2
    return {
        "copyPaste": {
            "source": {
                "sheetId": sheet_id,
                "startRowIndex": source_row,
                "endRowIndex": source_row + 1,
                "startColumnIndex": 10,
                "endColumnIndex": 11,
            },
            "destination": {
                "sheetId": sheet_id,
                "startRowIndex": row - 1,
                "endRowIndex": row,
                "startColumnIndex": 1,
                "endColumnIndex": 2,
            },
            "pasteType": "PASTE_FORMAT",
            "pasteOrientation": "NORMAL",
        }
    }


def update_targets_from_signal(signal_text: str) -> dict:
    """Parse signal and update (or append) target % in Google Sheet 'Signals' worksheet.
    - If NO valid targets → treat as 100% CASH and zero out Column C for ALL existing assets.
    - For NEW assets: full row copy (A:F) + explicitly fix Column D formula to =B2*C{row}
      (copy-paste was auto-incrementing B3 → B2, so we force the correct reference).
    - Column B uses the Long/Short direction and matching format from K2/K3.
    - Uses USER_ENTERED everywhere → no backticks.
    """
    targets = parse_signal_update(signal_text)

    spreadsheet_id = os.getenv("GOOGLE_DOCS_SHEET_ID")
    if not spreadsheet_id:
        raise ValueError("GOOGLE_DOCS_SHEET_ID not set in .env")

    gc = get_gspread_client()
    worksheet = gc.open_by_key(spreadsheet_id).worksheet("Signals")

    # Read current assets
    asset_rows = worksheet.get_values("A8:A")
    existing = {}  # asset -> row_index_1based
    for i, row in enumerate(asset_rows, start=8):
        if not row or not row[0]:
            break
        asset = str(row[0]).strip().upper()
        existing[asset] = i

    results = []

    # === 100% CASH CASE (no targets found) ===
    if not targets:
        if existing:
            zero_updates = []
            for row_num in existing.values():
                zero_updates.append({"range": f"C{row_num}", "values": [["0.0%"]]})
            worksheet.batch_update(zero_updates, value_input_option="USER_ENTERED")
            results.append(f"💰 100% CASH signal — zeroed {len(existing)} assets")
        else:
            results.append("💰 100% CASH signal — no assets to update")
        return {"status": "success", "message": " | ".join(results), "targets": {}}

    # === NORMAL CASE (assets in signal) ===
    updates = []
    new_rows_data = []
    direction_format_requests = []
    for asset, (pct, direction) in targets.items():
        pct_str = f"{pct * 100:.1f}%"
        if asset in existing:
            row = existing[asset]
            updates.append(
                {"range": f"B{row}:C{row}", "values": [[direction, pct_str]]}
            )
            direction_format_requests.append(
                _direction_format_request(worksheet.id, row, direction)
            )
        else:
            new_rows_data.append([asset, direction, pct_str])

    # === ZERO OUT ASSETS THAT DISAPPEARED FROM THIS SIGNAL ===
    # Always do this in the normal case, regardless of whether we updated any existing rows.
    dropped_assets = set(existing.keys()) - set(targets.keys())
    if dropped_assets:
        zero_updates = []
        for asset in dropped_assets:
            row = existing[asset]
            zero_updates.append({"range": f"C{row}", "values": [["0.0%"]]})
        worksheet.batch_update(zero_updates, value_input_option="USER_ENTERED")
        results.append(f"✅ Zeroed {len(zero_updates)} asset(s)")

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
                        "pasteType": "PASTE_NORMAL",
                        "pasteOrientation": "NORMAL",
                    }
                }
                worksheet.spreadsheet.batch_update({"requests": [copy_request]})

            # === Overwrite A:C with new data ===
            asset_name, direction, pct_str = new_asset_data
            worksheet.update(
                f"A{current_new_row}:C{current_new_row}",
                [[asset_name, direction, pct_str]],
                value_input_option="USER_ENTERED",
            )

            # === CRITICAL FIX: Force Column D to =B2*C{new_row} ===
            worksheet.update(
                f"D{current_new_row}",
                [[f"=B2*C{current_new_row}"]],
                value_input_option="USER_ENTERED",
            )
            direction_format_requests.append(
                _direction_format_request(worksheet.id, current_new_row, direction)
            )

        results.append(
            f"✅ Added {len(new_rows_data)} new asset(s) with full row copy + fixed D formula"
        )

    if direction_format_requests:
        worksheet.spreadsheet.batch_update({"requests": direction_format_requests})

    total = sum(pct for pct, _ in targets.values())
    results.append(f"📊 New targets sum: {total:.1%} ({len(targets)} assets)")

    return {"status": "success", "message": " | ".join(results), "targets": targets}
