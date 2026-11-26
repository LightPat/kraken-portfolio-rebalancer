import os
import time
import math
import logging
import httpx
from pathlib import Path
from typing import List, Dict, Any
from sheets import (
    get_target_allocations,
    get_desired_cash_reserve,
    get_rsps_allocation_pct,
    get_sdca_cash_on_kraken,
    get_sdca_sheet_values,
    update_current_allocations_in_sheet,
    update_sdca_change_in_sheet,
)
from kraken import (
    fetch_portfolio,
    fetch_margin_positions,
    get_kraken_exchange,
    get_open_orders,
    cancel_order,
    cancel_open_orders,
    create_market_order,
    create_post_only_limit_order,
    get_safe_post_only_price,
    is_stable_coin,
    get_best_trading_symbol,
    validate_quote_balances,
    _quote_currency,
    _margin_symbol,
    fetch_order,
    get_order_remaining,
    get_free_balance,
    get_btc_usd_price,
)
from bot import CRYPTO_DECIMALS, PRICE_DECIMALS

ORDER_TIMEOUT_SECONDS = 300
ORDER_POLL_INTERVAL_SECONDS = 3
SHORT_ACTIONS = {
    "open_short",
    "increase_short",
    "reduce_short",
    "cover_short",
}

CANCEL_FLAG_FILE = "cancel_rebalance.flag"
PROGRESS_EDIT_INTERVAL_SECONDS = 2.0
PROGRESS_HTTP_TIMEOUT_SECONDS = 5.0
TELEGRAM_TOKEN_ENV = "TELEGRAM_KRAKEN_PORTFOLIO_REBALANCER_BOT_HTTP_TOKEN"
logger = logging.getLogger(__name__)


class TelegramProgressReporter:
    def __init__(self, chat_id: int | None, message_id: int | None):
        self.chat_id = chat_id
        self.message_id = message_id
        self.token = os.getenv(TELEGRAM_TOKEN_ENV)
        self.last_edit_at: float | None = (
            time.monotonic()
            if self.token and chat_id is not None and message_id is not None
            else None
        )
        self.current_trade: str | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id is not None and self.message_id)

    def update(self, text: str, final: bool = False) -> bool:
        if not self.enabled:
            return False

        elapsed = (
            PROGRESS_EDIT_INTERVAL_SECONDS
            if self.last_edit_at is None
            else time.monotonic() - self.last_edit_at
        )
        if final and elapsed < PROGRESS_EDIT_INTERVAL_SECONDS:
            time.sleep(PROGRESS_EDIT_INTERVAL_SECONDS - elapsed)
        elif not final and elapsed < PROGRESS_EDIT_INTERVAL_SECONDS:
            return False

        self.last_edit_at = time.monotonic()
        response = httpx.post(
            f"https://api.telegram.org/bot{self.token}/editMessageText",
            json={
                "chat_id": self.chat_id,
                "message_id": self.message_id,
                "text": text,
            },
            timeout=PROGRESS_HTTP_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        if not response.json().get("ok", False):
            raise RuntimeError("Telegram rejected the progress message edit")
        return True


def report_progress(
    progress: TelegramProgressReporter | None, text: str, final: bool = False
) -> bool:
    """Edit a Telegram progress message without affecting trade execution."""
    if progress is None:
        return False
    try:
        return progress.update(text, final=final)
    except Exception:
        logger.warning("Failed to edit Telegram progress message", exc_info=True)
        return False


def _trade_progress_label(trade: Dict, index: int, total: int) -> str:
    action = trade["action"]
    asset = trade.get("asset") or trade.get("symbol", "asset").split("/")[0]
    if action in {"reduce_short", "cover_short"}:
        description = f"Closing {asset} short"
    elif action in {"reduce_long", "close_long"}:
        description = f"Closing {asset} long"
    elif action in {"open_short", "increase_short"}:
        description = f"Opening {asset} short"
    else:
        description = f"Opening {asset} long"
    return f"{description} ({index}/{total})"


def request_cancel_rebalance():
    Path(CANCEL_FLAG_FILE).touch(exist_ok=True)


def reset_cancel_rebalance():
    try:
        os.unlink(CANCEL_FLAG_FILE)
    except FileNotFoundError:
        pass


def is_rebalance_cancel_requested() -> bool:
    return os.path.exists(CANCEL_FLAG_FILE)


def generate_rebalance_plan(
    progress: TelegramProgressReporter | None = None,
) -> Dict[str, Any]:
    report_progress(progress, "Refreshing allocations…")
    update_current_allocations_in_sheet()
    report_progress(progress, "Allocations refreshed; building RSPS plan…")
    exchange = get_kraken_exchange()
    (
        spot_value_by_asset,
        short_notional_by_asset,
        short_used_margin_by_asset,
        long_margin_notional_by_asset,
        total_value,
        stable_breakdown,
    ) = fetch_portfolio()
    targets = get_target_allocations()
    margin_positions = fetch_margin_positions()
    desired_reserve = get_desired_cash_reserve()
    rsps_allocation_pct = get_rsps_allocation_pct()
    sdca_values = get_sdca_sheet_values()
    sdca_cash_on_kraken = get_sdca_cash_on_kraken()
    stable_breakdown = dict(stable_breakdown)
    stable_breakdown["USD"] = max(
        0.0,
        stable_breakdown.get("USD", 0.0) - sdca_cash_on_kraken,
    )

    if sdca_values["kraken_btc"] > 0:
        btc_price = get_btc_usd_price()
        spot_value_by_asset = dict(spot_value_by_asset)
        spot_value_by_asset["BTC"] = max(
            0.0,
            spot_value_by_asset.get("BTC", 0.0)
            - sdca_values["kraken_btc"] * btc_price,
        )

    investable_value = calculate_rsps_investable_value(
        total_value,
        desired_reserve,
        sdca_cash_on_kraken,
    )
    closing_plan: List[Dict] = []
    opening_plan: List[Dict] = []
    threshold = 15  # ignore tiny trades
    positions_by_asset: Dict[str, List[Dict]] = {}
    for position in margin_positions:
        positions_by_asset.setdefault(position["asset"], []).append(position)

    def append_trade(
        asset: str,
        action: str,
        amount_usd: float,
        amount_base: float,
        price: float,
        symbol: str,
        capital_usd: float,
        margin: bool = False,
    ) -> None:
        if amount_usd < threshold:
            return
        trade = {
            "asset": asset,
            "action": action,
            "amount_usd": amount_usd,
            "amount_base": amount_base,
            "capital_usd": capital_usd,
            "price": price,
            "symbol": symbol,
            "quote": _quote_currency(symbol),
            "margin": margin,
        }
        if action in {"open_long", "increase_long", "open_short", "increase_short"}:
            opening_plan.append(trade)
        else:
            closing_plan.append(trade)

    for asset, target in targets.items():
        target_pct = target["pct"]
        direction = target["direction"]
        target_capital_usd = target_pct * investable_value
        asset_positions = positions_by_asset.get(asset, [])
        short_positions = [p for p in asset_positions if p["side"] == "short"]
        margin_longs = [p for p in asset_positions if p["side"] == "long"]
        current_spot_usd = spot_value_by_asset.get(asset, 0.0)
        current_short_usd = short_notional_by_asset.get(
            asset, sum(p["notional_usd"] for p in short_positions)
        )
        current_short_margin_usd = short_used_margin_by_asset.get(
            asset, sum(p["used_margin_usd"] for p in short_positions)
        )
        if is_stable_coin(asset):
            continue

        try:
            symbol = get_best_trading_symbol(asset)
            ticker = exchange.fetch_ticker(symbol)
            price = ticker.get("last") or ticker.get("close")
            if not price or price <= 0:
                raise ValueError(f"No valid price available for {symbol}")

            if direction == "Short":
                if current_spot_usd > 0:
                    append_trade(
                        asset,
                        "close_long",
                        current_spot_usd,
                        current_spot_usd / price,
                        price,
                        symbol,
                        capital_usd=current_spot_usd,
                    )

                short_capital_delta = target_capital_usd - current_short_margin_usd
                if short_capital_delta < 0:
                    capital_to_release = min(
                        -short_capital_delta, current_short_margin_usd
                    )
                    remaining_capital = capital_to_release
                    for position in short_positions:
                        if remaining_capital <= 0:
                            break
                        position_margin = position["used_margin_usd"]
                        capital_usd = min(position_margin, remaining_capital)
                        amount_base = min(
                            position["size_base"] * capital_usd / position_margin,
                            position["size_base"],
                        )
                        amount_usd = amount_base * position["mark_price"]
                        if amount_base <= 0:
                            continue
                        append_trade(
                            asset,
                            "cover_short"
                            if amount_base >= position["size_base"] - 1e-12
                            else "reduce_short",
                            amount_usd,
                            amount_base,
                            position["mark_price"],
                            position["symbol"],
                            margin=True,
                            capital_usd=capital_usd,
                        )
                        remaining_capital -= capital_usd
                elif short_capital_delta > 0:
                    if margin_longs:
                        print(
                            f"⚠️ Refusing to open {asset} short while a margin "
                            "long position remains."
                        )
                    else:
                        append_trade(
                            asset,
                            "open_short"
                            if current_short_usd == 0
                            else "increase_short",
                            short_capital_delta * 2,
                            short_capital_delta * 2 / price,
                            price,
                            symbol,
                            margin=True,
                            capital_usd=short_capital_delta,
                        )
            else:
                for position in short_positions:
                    append_trade(
                        asset,
                        "cover_short",
                        position["notional_usd"],
                        position["size_base"],
                        position["mark_price"],
                        position["symbol"],
                        margin=True,
                        capital_usd=position["used_margin_usd"],
                    )

                delta_long_usd = target_capital_usd - current_spot_usd
                if delta_long_usd < -threshold:
                    amount_usd = min(-delta_long_usd, current_spot_usd)
                    append_trade(
                        asset,
                        "close_long" if target_capital_usd == 0 else "reduce_long",
                        amount_usd,
                        amount_usd / price,
                        price,
                        symbol,
                        capital_usd=amount_usd,
                    )
                elif delta_long_usd > threshold:
                    append_trade(
                        asset,
                        "open_long" if current_spot_usd == 0 else "increase_long",
                        delta_long_usd,
                        delta_long_usd / price,
                        price,
                        symbol,
                        capital_usd=delta_long_usd,
                    )
        except Exception as e:
            print(f"❌ Could not create trade for {asset}: {e}")

    current_allocated_capital = (
        sum(spot_value_by_asset.values())
        + sum(short_used_margin_by_asset.values())
        + sum(long_margin_notional_by_asset.values())
    )
    released_capital = sum(
        trade["capital_usd"]
        for trade in closing_plan
        if trade["action"]
        in {"reduce_short", "cover_short", "reduce_long", "close_long"}
    )
    projected_capital = max(0.0, current_allocated_capital - released_capital)

    affordable_openings = []
    refused_openings = []
    for trade in opening_plan:
        if projected_capital + trade["capital_usd"] > investable_value + 1e-8:
            refused_openings.append(trade)
            continue
        affordable_openings.append(trade)
        projected_capital += trade["capital_usd"]
    if refused_openings:
        refused_assets = ", ".join(
            sorted({trade["asset"] for trade in refused_openings})
        )
        print(
            f"⚠️ Refusing new opens for {refused_assets}: combined long notional "
            "and short used margin would exceed investable capital "
            f"(${investable_value:,.2f})."
        )

    plan = closing_plan + affordable_openings
    current_stables_total = sum(stable_breakdown.values())
    return {
        "total_value_usd": total_value,
        "desired_reserve": desired_reserve,
        "rsps_allocation_pct": rsps_allocation_pct,
        "investable_value": investable_value,
        "current_stables_total": current_stables_total,
        "current_portfolio": {k: v for k, v in spot_value_by_asset.items()},
        "plan": plan,
        "dry_run": os.getenv("DRY_RUN", "false").lower() == "true",
    }


def calculate_rsps_investable_value(
    total_equity: float,
    desired_reserve: float,
    sdca_cash_on_kraken: float,
) -> float:
    """Calculate RSPS investable capital as Signals!B2: E2 - I3 - Q6."""
    if not all(
        math.isfinite(value)
        for value in (
            total_equity,
            desired_reserve,
            sdca_cash_on_kraken,
        )
    ):
        raise ValueError("RSPS capital inputs must be finite numbers")
    if desired_reserve < 0 or sdca_cash_on_kraken < 0:
        raise ValueError("RSPS reserve and SDCA Kraken cash must be non-negative")
    return max(
        0.0,
        total_equity - desired_reserve - sdca_cash_on_kraken,
    )


def generate_sdca_plan(
    progress: TelegramProgressReporter | None = None,
) -> Dict:
    report_progress(progress, "Refreshing allocations for the SDCA plan…")
    update_current_allocations_in_sheet()
    report_progress(progress, "Allocations refreshed; building SDCA plan…")
    sdca = get_sdca_sheet_values()
    change_pct = sdca["change_pct"]
    dry_run = os.getenv("DRY_RUN", "false").lower() == "true"
    action = "buy" if change_pct > 0 else "sell" if change_pct < 0 else None
    if action is None:
        return {
            "action": None,
            "price": 0.0,
            "amount_usd": 0.0,
            "amount_base": 0.0,
            "target_btc": 0.0,
            "unavailable_btc": 0.0,
            "change_pct": change_pct,
            "cash_usd": sdca["cash_usd"],
            "kraken_btc": sdca["kraken_btc"],
            "cold_btc": sdca["cold_btc"],
            "quote_warnings": [],
            "dry_run": dry_run,
        }

    exchange = get_kraken_exchange()
    symbol = get_best_trading_symbol("BTC")
    if _quote_currency(symbol) != "USD":
        raise ValueError(f"SDCA requires a BTC/USD market; selected {symbol}")
    ticker = exchange.fetch_ticker(symbol)
    price = ticker.get("last") or ticker.get("close")
    if not price or price <= 0:
        raise ValueError(f"No valid price available for {symbol}")

    if action == "buy":
        amount_usd = change_pct * sdca["cash_usd"]
        amount_base = amount_usd / price
        target_btc = 0.0
        unavailable_btc = 0.0
        quote_warnings = []
        if not dry_run:
            quote_warnings = validate_quote_balances(
                [
                    {
                        "asset": "BTC",
                        "action": "buy",
                        "amount_usd": amount_usd,
                        "symbol": symbol,
                        "quote": _quote_currency(symbol),
                        "margin": False,
                    }
                ]
            )
    elif action == "sell":
        target_btc = abs(change_pct) * (sdca["kraken_btc"] + sdca["cold_btc"])
        free_btc = get_free_balance("BTC")
        amount_base = min(target_btc, free_btc, sdca["kraken_btc"])
        unavailable_btc = max(0.0, target_btc - amount_base)
        amount_usd = amount_base * price
        quote_warnings = []
    else:
        target_btc = 0.0
        unavailable_btc = 0.0
        amount_base = 0.0
        amount_usd = 0.0
        quote_warnings = []

    return {
        "action": action,
        "symbol": symbol,
        "price": float(price),
        "amount_usd": amount_usd,
        "amount_base": amount_base,
        "target_btc": target_btc,
        "unavailable_btc": unavailable_btc,
        "change_pct": change_pct,
        "cash_usd": sdca["cash_usd"],
        "kraken_btc": sdca["kraken_btc"],
        "cold_btc": sdca["cold_btc"],
        "quote_warnings": quote_warnings,
        "dry_run": dry_run,
    }


def _order_fee_amounts(order: Dict, quote: str = "USD") -> tuple[float, float]:
    quote_fee = 0.0
    base_fee = 0.0
    fees = order.get("fees") or ([order["fee"]] if order.get("fee") else [])
    for fee in fees:
        amount = float(fee.get("cost") or 0.0)
        currency = str(fee.get("currency") or "").upper()
        if currency == quote:
            quote_fee += amount
        elif currency == "BTC":
            base_fee += amount
    return quote_fee, base_fee


def _collect_sdca_order_fills(
    orders: List[Dict], symbol: str, action: str
) -> tuple[float, float]:
    quote_amount = 0.0
    btc_delta = 0.0
    for submitted in orders:
        order = submitted
        order_id = submitted.get("id") or submitted.get("order_id")
        if order_id:
            refreshed = fetch_order(order_id, symbol)
            if refreshed:
                order = {**submitted, **refreshed}

        filled = float(order.get("filled") or 0.0)
        cost = float(order.get("cost") or 0.0)
        average = float(order.get("average") or 0.0)
        if filled > 0 and cost <= 0 and average > 0:
            cost = filled * average
        if filled > 0 and cost <= 0:
            raise RuntimeError(
                f"Kraken did not report quote value for filled SDCA order {order_id}"
            )

        quote_fee, base_fee = _order_fee_amounts(order)
        if action == "buy":
            quote_amount += cost + quote_fee
            btc_delta += filled - base_fee
        else:
            quote_amount += cost - quote_fee
            btc_delta -= filled + base_fee
    return quote_amount, btc_delta


def execute_sdca_trade(
    progress: TelegramProgressReporter | None = None,
) -> Dict[str, Any]:
    plan = generate_sdca_plan(progress=progress)
    action = plan["action"]
    results: List[str] = []
    if action is None or plan["amount_base"] <= 0:
        results.append("ℹ️ SDCA Change % Today is zero; nothing to do.")
        return {"results": results, "plan": plan, "dry_run": plan["dry_run"]}
    if plan.get("quote_warnings"):
        results.extend(plan["quote_warnings"])
        results.append(
            "⏸️ SDCA buy paused until the required USD funds are available. "
            "Convert or fund USD, then generate a fresh plan."
        )
        return {
            "status": "blocked",
            "results": results,
            "plan": plan,
            "dry_run": plan["dry_run"],
        }
    if plan["dry_run"]:
        results.append(
            f"🧪 DRY-RUN: Would {action} {plan['amount_base']:.8f} BTC "
            f"(~${plan['amount_usd']:.2f}); SDCA cash remains unchanged."
        )
        if action == "sell" and plan["unavailable_btc"] > 0:
            results.append(
                f"📥 Move {plan['unavailable_btc']:.8f} BTC from cold storage "
                "to Kraken to complete the intended sale."
            )
        report_progress(progress, "Updating sheet after SDCA dry-run…")
        update_current_allocations_in_sheet()
        return {"results": results, "plan": plan, "dry_run": True}

    if action == "buy":
        free_usd = get_free_balance("USD")
        if plan["amount_usd"] > free_usd + 1e-8:
            raise ValueError(
                f"SDCA buy requires ${plan['amount_usd']:.2f}, but only "
                f"${free_usd:.2f} USD is available on Kraken."
            )
    else:
        free_btc = get_free_balance("BTC")
        plan["amount_base"] = min(
            plan["target_btc"], free_btc, plan["kraken_btc"]
        )
        plan["unavailable_btc"] = max(
            0.0, plan["target_btc"] - plan["amount_base"]
        )
        if plan["amount_base"] <= 0:
            results.append(
                f"📥 Move {plan['target_btc']:.8f} BTC from cold storage "
                "to Kraken before selling."
            )
            report_progress(progress, "Updating sheet…")
            update_current_allocations_in_sheet()
            return {"results": results, "plan": plan, "dry_run": False}

    exchange = get_kraken_exchange()
    ticker = exchange.fetch_ticker(plan["symbol"])
    side = "buy" if action == "buy" else "sell"
    limit_price = get_safe_post_only_price(ticker, side)
    if limit_price <= 0:
        raise ValueError(f"No valid post-only price available for {plan['symbol']}")
    amount_base = (
        plan["amount_usd"] / limit_price
        if action == "buy"
        else plan["amount_base"]
    )
    amount_base = math.floor(amount_base * 100_000_000) / 100_000_000
    if amount_base <= 0:
        raise ValueError("SDCA order amount is below Kraken's BTC precision")

    trade_label = f"SDCA {action.upper()} BTC (1/1)"
    if progress is not None:
        progress.current_trade = trade_label
    report_progress(progress, f"{trade_label} — placing post-only order…")
    initial_order = create_post_only_limit_order(
        plan["symbol"], side, amount_base, limit_price
    )
    report_progress(
        progress,
        f"{trade_label} — post-only order placed; waiting for fill…",
    )
    orders = [initial_order]
    initial_status = str(initial_order.get("status", "")).lower()
    if (
        initial_order.get("id")
        and (
            initial_status in {"open", "pending", "new"}
            or get_order_remaining(initial_order) > 0
            or not initial_status
        )
    ):
        order_reference = {
            "order_id": initial_order["id"],
            "symbol": plan["symbol"],
            "side": side,
            "amount_base": amount_base,
            "quote": "USD",
            "asset": "BTC",
            "action": action,
            "margin": False,
            "reduce_only": False,
            "progress_label": trade_label,
        }
        fallback_orders = _wait_for_order_completion(
            [order_reference],
            results,
            timeout_seconds=ORDER_TIMEOUT_SECONDS,
            poll_interval_seconds=ORDER_POLL_INTERVAL_SECONDS,
            progress=progress,
        )
        if fallback_orders:
            orders.extend(
                _cancel_and_market_fallback(
                    fallback_orders, results, progress=progress
                )
            )

    actual_quote_amount, btc_delta = _collect_sdca_order_fills(
        orders, plan["symbol"], action
    )
    if actual_quote_amount <= 0 or btc_delta == 0:
        raise RuntimeError("Kraken reported no completed SDCA BTC trade")

    report_progress(progress, f"{trade_label} — filled.")
    report_progress(progress, "Updating sheet…")
    update_current_allocations_in_sheet(
        sdca_btc_adjustment=btc_delta, force_cold_balance_refresh=True
    )
    update_sdca_change_in_sheet(0.0)
    report_progress(progress, "Sheet updated; Change % Today cleared.")

    results.append(
        f"✅ SDCA {action.upper()} filled: {abs(btc_delta):.8f} BTC, "
        f"${actual_quote_amount:.2f} {'spent' if action == 'buy' else 'proceeds'}."
    )
    results.append("✅ Change % Today cleared.")
    if action == "buy":
        results.append(
            f"📤 Withdraw approximately {abs(btc_delta):.8f} BTC "
            "to cold storage when ready."
        )
    elif plan["target_btc"] > abs(btc_delta) + 1e-8:
        additional_btc = plan["target_btc"] - abs(btc_delta)
        results.append(
            f"📥 Move an additional {additional_btc:.8f} BTC from "
            "cold storage to Kraken to complete the intended sale."
        )
    return {
        "results": results,
        "plan": plan,
        "actual_quote_amount_usd": actual_quote_amount,
        "btc_delta": btc_delta,
        "dry_run": False,
    }


def _submit_trade_orders(
    exchange,
    trades: List[Dict],
    dry_run: bool,
    results: List[str],
    progress: TelegramProgressReporter | None = None,
    progress_labels: List[str] | None = None,
) -> List[Dict]:
    submitted_orders = []
    for index, trade in enumerate(trades, start=1):
        progress_label = (
            progress_labels[index - 1]
            if progress_labels is not None
            else _trade_progress_label(trade, index, len(trades))
        )
        if progress is not None:
            progress.current_trade = progress_label
        try:
            report_progress(
                progress, f"{progress_label} — preparing post-only order…"
            )
            ticker = exchange.fetch_ticker(trade["symbol"])
            side = {
                "open_short": "sell",
                "increase_short": "sell",
                "reduce_short": "buy",
                "cover_short": "buy",
                "open_long": "buy",
                "increase_long": "buy",
                "reduce_long": "sell",
                "close_long": "sell",
            }[trade["action"]]
            margin = trade["action"] in SHORT_ACTIONS
            order_symbol = (
                _margin_symbol(trade["symbol"]) if margin else trade["symbol"]
            )
            reduce_only = trade["action"] in {"reduce_short", "cover_short"}
            limit_price = get_safe_post_only_price(ticker, side)
            amount_base = trade["amount_base"]
            if trade["action"] in {"open_short", "increase_short"}:
                max_amount = trade["capital_usd"] * 2 / limit_price
                if amount_base > max_amount:
                    amount_base = max_amount

            if dry_run:
                capital = (
                    f" (capital ${trade['capital_usd']:,.2f})"
                    if trade.get("capital_usd") is not None
                    else ""
                )
                results.append(
                    f"🧪 DRY-RUN: Would {trade['action']} "
                    f"{round(amount_base, CRYPTO_DECIMALS)} {trade['asset']}"
                    f"{' on 2x margin' if margin else ''}{capital} "
                    f"@ ~${round(limit_price, PRICE_DECIMALS)}"
                )
                continue

            report_progress(
                progress, f"{progress_label} — placing post-only order…"
            )
            order = create_post_only_limit_order(
                trade["symbol"],
                side,
                amount_base,
                limit_price,
                margin=margin,
                reduce_only=reduce_only,
            )
            results.append(
                f"✅ {trade['action'].upper()} "
                f"{round(amount_base, CRYPTO_DECIMALS)} {trade['asset']} "
                f"{'on 2x margin ' if margin else ''}"
                f"@ LIMIT ~${round(limit_price, PRICE_DECIMALS)} "
                f"(ID: {order.get('id')})"
            )
            report_progress(
                progress,
                f"{progress_label} — post-only order placed; waiting for fill…",
            )

            submitted_orders.append(
                {
                    "order_id": order.get("id"),
                    "symbol": order_symbol,
                    "side": side,
                    "amount_base": amount_base,
                    "quote": trade["quote"],
                    "asset": trade["asset"],
                    "action": trade["action"],
                    "margin": margin,
                    "reduce_only": reduce_only,
                    "progress_label": progress_label,
                }
            )
        except Exception as e:
            results.append(f"❌ Failed {trade['action']} {trade['asset']}: {e}")
            report_progress(progress, f"❌ {progress_label} — order failed.")
    return submitted_orders


def _wait_for_order_completion(
    submitted_orders: List[Dict],
    results: List[str],
    timeout_seconds: int,
    poll_interval_seconds: int,
    progress: TelegramProgressReporter | None = None,
) -> List[Dict]:
    if not submitted_orders:
        return []

    symbols = list({order["symbol"] for order in submitted_orders})
    start_time = time.monotonic()

    remaining_orders = [order.copy() for order in submitted_orders]
    fallback_orders = []
    while time.monotonic() - start_time < timeout_seconds:
        if is_rebalance_cancel_requested():
            results.append(
                "⏹️ Rebalance cancellation requested, stopping order monitoring."
            )
            active_trade = (
                remaining_orders[0].get("progress_label")
                if remaining_orders
                else getattr(progress, "current_trade", None)
            )
            if active_trade:
                results.append(
                    f"⏹️ Cancellation received during {active_trade}."
                )
            report_progress(
                progress,
                f"⏹️ Rebalance cancelled during {active_trade}."
                if active_trade
                else "⏹️ Rebalance cancelled.",
            )
            return fallback_orders + remaining_orders

        open_orders = get_open_orders(symbols)
        open_ids = {order["id"] for order in open_orders}
        still_open_orders = []
        for order in remaining_orders:
            if order["order_id"] in open_ids:
                still_open_orders.append(order)
                continue

            order_info = fetch_order(order["order_id"], order["symbol"])
            status = str(order_info.get("status", "")).lower() if order_info else ""
            if (
                order_info is None
                or status in {"canceled", "cancelled", "expired"}
                or get_order_remaining(order_info) > 0
            ):
                fallback_orders.append(order)
            else:
                report_progress(
                    progress,
                    f"{order.get('progress_label', order['asset'])} — filled.",
                )

        remaining_orders = still_open_orders

        if not remaining_orders:
            return fallback_orders

        time.sleep(poll_interval_seconds)

    return fallback_orders + remaining_orders


def _cancel_and_market_fallback(
    orders: List[Dict],
    results: List[str],
    progress: TelegramProgressReporter | None = None,
) -> List[Dict]:
    market_orders = []
    for order in orders:
        if not order.get("order_id"):
            continue
        progress_label = order.get("progress_label", order["asset"])
        if progress is not None:
            progress.current_trade = progress_label

        order_info = fetch_order(order["order_id"], order["symbol"])
        remaining = (
            get_order_remaining(order_info) if order_info else order["amount_base"]
        )
        if remaining <= 0:
            continue

        report_progress(
            progress, f"{progress_label} — falling back to market…"
        )
        try:
            cancel_order(order["order_id"])
            results.append(
                f"⏳ Timed out: cancelled stale {order['side']} order for {order['asset']}."
            )
        except Exception as e:
            results.append(f"⚠️ Failed to cancel stale order {order['order_id']}: {e}")

        try:
            market = create_market_order(
                order["symbol"],
                order["side"],
                remaining,
                margin=order.get("margin", False),
                reduce_only=order.get("reduce_only", False),
            )
            market_orders.append(market)
            results.append(
                f"🚀 MARKET {order.get('action', order['side']).upper()} for "
                f"{order['asset']} remaining {round(remaining, CRYPTO_DECIMALS)} "
                f"@ {order['symbol']} (ID: {market.get('id')})"
            )
            report_progress(
                progress, f"{progress_label} — market fallback order placed."
            )
            market_filled = float(market.get("filled") or 0.0)
            market_amount = float(market.get("amount") or remaining)
            if (
                str(market.get("status", "")).lower() == "closed"
                and market_filled >= market_amount
            ):
                report_progress(progress, f"{progress_label} — filled.")
        except Exception as e:
            results.append(
                f"❌ Failed market fallback for {order['asset']} ({order['symbol']}): {e}"
            )
            report_progress(
                progress, f"❌ {progress_label} — market fallback failed."
            )
    return market_orders


def execute_trades(
    plan: List[Dict],
    progress: TelegramProgressReporter | None = None,
):
    dry_run = os.getenv("DRY_RUN", "false").lower() == "true"
    exchange = get_kraken_exchange()
    results = []

    if not plan:
        return results
    reserved_sdca_cash = {"USD": get_sdca_cash_on_kraken()}
    progress_position = 0
    progress_total = max(1, len(plan))

    if not dry_run:
        try:
            symbols = list(
                dict.fromkeys(
                    symbol
                    for trade in plan
                    for symbol in (
                        trade["symbol"],
                        _margin_symbol(trade["symbol"]),
                    )
                )
            )
            cancelled = cancel_open_orders(symbols)
            for order_id in cancelled:
                results.append(f"🧹 Cancelled existing order {order_id}")
        except Exception as e:
            results.append(f"⚠️ Order cleanup failed: {e}")

    def execute_phase(trades: List[Dict]) -> bool:
        nonlocal progress_position, progress_total
        progress_total = max(progress_total, progress_position + len(trades))
        progress_labels = [
            _trade_progress_label(trade, progress_position + index, progress_total)
            for index, trade in enumerate(trades, start=1)
        ]
        progress_position += len(trades)
        submitted_orders = _submit_trade_orders(
            exchange,
            trades,
            dry_run,
            results,
            progress=progress,
            progress_labels=progress_labels,
        )
        if dry_run:
            return True
        orders_needing_fallback = _wait_for_order_completion(
            submitted_orders,
            results,
            timeout_seconds=ORDER_TIMEOUT_SECONDS,
            poll_interval_seconds=ORDER_POLL_INTERVAL_SECONDS,
            progress=progress,
        )
        if orders_needing_fallback:
            _cancel_and_market_fallback(
                orders_needing_fallback, results, progress=progress
            )
        cancelled = is_rebalance_cancel_requested()
        if not cancelled and progress is not None:
            progress.current_trade = None
        return not cancelled

    def report_cancellation(fallback_message: str) -> None:
        active_trade = getattr(progress, "current_trade", None)
        if active_trade:
            detail = f"⏹️ Cancellation received during {active_trade}."
            if detail not in results:
                results.append(detail)
            report_progress(
                progress, f"⏹️ Rebalance cancelled during {active_trade}."
            )
        else:
            report_progress(progress, fallback_message)

    short_reductions = [
        trade for trade in plan if trade["action"] in {"reduce_short", "cover_short"}
    ]
    long_reductions = [
        trade for trade in plan if trade["action"] in {"reduce_long", "close_long"}
    ]

    if short_reductions:
        cover_warnings = validate_quote_balances(
            short_reductions, reserved_quote_balances=reserved_sdca_cash
        )
        if cover_warnings:
            results.extend(cover_warnings)
            results.append(
                "⏸️ Short reductions paused until the required quote funds are available."
            )
            return results

    if short_reductions and not execute_phase(short_reductions):
        results.append("⏹️ Rebalance cancelled after reducing or covering shorts.")
        report_cancellation("⏹️ Rebalance cancelled after reducing or covering shorts.")
        return results
    if long_reductions and not execute_phase(long_reductions):
        results.append("⏹️ Rebalance cancelled after reducing spot longs.")
        report_cancellation("⏹️ Rebalance cancelled after reducing spot longs.")
        return results

    if dry_run:
        remaining_plan = plan
    else:
        report_progress(progress, "Recalculating remaining RSPS targets…")
        results.append(
            "🔄 Recalculating remaining targets from the post-reduction portfolio..."
        )
        try:
            remaining_plan = generate_rebalance_plan(progress=progress).get("plan", [])
        except Exception as e:
            results.append(f"❌ Failed to regenerate the remaining plan: {e}")
            report_progress(progress, f"❌ Failed to regenerate remaining plan: {e}")
            return results

    opening_trades = [
        trade
        for trade in remaining_plan
        if trade["action"]
        in {"open_short", "increase_short", "open_long", "increase_long"}
    ]
    short_openings = [
        trade
        for trade in opening_trades
        if trade["action"] in {"open_short", "increase_short"}
    ]
    long_openings = [
        trade
        for trade in opening_trades
        if trade["action"] in {"open_long", "increase_long"}
    ]

    if not dry_run and opening_trades:
        fresh_portfolio = fetch_portfolio()
        fresh_spot_values = fresh_portfolio[0]
        fresh_margin_positions = fetch_margin_positions()
        short_margin_assets = {
            position["asset"]
            for position in fresh_margin_positions
            if position["side"] == "short"
        }
        long_margin_assets = {
            position["asset"]
            for position in fresh_margin_positions
            if position["side"] == "long"
        }
        allowed_short_openings = []
        for trade in short_openings:
            if (
                fresh_spot_values.get(trade["asset"], 0.0) > 0
                or trade["asset"] in long_margin_assets
            ):
                results.append(
                    f"⏸️ Refusing to open {trade['asset']} short while a spot or "
                    "margin long remains."
                )
                continue
            allowed_short_openings.append(trade)
        short_openings = allowed_short_openings
        long_openings = [
            trade
            for trade in long_openings
            if trade["asset"] not in short_margin_assets
        ]
        blocked_long_assets = {
            trade["asset"]
            for trade in opening_trades
            if trade["action"] in {"open_long", "increase_long"}
            and trade["asset"] in short_margin_assets
        }
        for asset in sorted(blocked_long_assets):
            results.append(
                f"⏸️ Refusing to open {asset} long while a margin short remains."
            )

    spending_trades = short_openings + long_openings
    if spending_trades:
        opening_warnings = validate_quote_balances(
            spending_trades, reserved_quote_balances=reserved_sdca_cash
        )
        if opening_warnings:
            results.extend(opening_warnings)
            results.append(
                "⏸️ New positions paused until the required quote funds are available."
            )
            return results

    if short_openings and not execute_phase(short_openings):
        results.append("⏹️ Rebalance cancelled after opening or increasing shorts.")
        report_cancellation("⏹️ Rebalance cancelled after opening or increasing shorts.")
        return results
    if long_openings and not execute_phase(long_openings):
        results.append("⏹️ Rebalance cancelled after opening or increasing longs.")
        report_cancellation("⏹️ Rebalance cancelled after opening or increasing longs.")
        return results

    if is_rebalance_cancel_requested():
        results.append(
            "⏹️ Rebalance has been cancelled. No further trades will be placed."
        )
        report_cancellation(
            "⏹️ Rebalance has been cancelled. No further trades will be placed."
        )

    return results
