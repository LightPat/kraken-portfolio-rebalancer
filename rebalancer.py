import os
import time
import math
from pathlib import Path
from typing import List, Dict, Any
from sheets import (
    get_target_allocations,
    get_desired_cash_reserve,
    get_rsps_allocation_pct,
    get_sdca_sheet_values,
    get_btc_cold_balance,
    update_sdca_values,
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


def request_cancel_rebalance():
    Path(CANCEL_FLAG_FILE).touch(exist_ok=True)


def reset_cancel_rebalance():
    try:
        os.unlink(CANCEL_FLAG_FILE)
    except FileNotFoundError:
        pass


def is_rebalance_cancel_requested() -> bool:
    return os.path.exists(CANCEL_FLAG_FILE)


def generate_rebalance_plan() -> Dict[str, Any]:
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
    stable_breakdown = dict(stable_breakdown)
    stable_breakdown["USD"] = max(
        0.0,
        stable_breakdown.get("USD", 0.0) - max(0.0, sdca_values["cash_usd"]),
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
        total_value, rsps_allocation_pct, desired_reserve
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
    total_equity: float, rsps_allocation_pct: float, desired_reserve: float
) -> float:
    """Calculate RSPS-only capital, matching Signals!B2."""
    if not all(
        math.isfinite(value)
        for value in (total_equity, rsps_allocation_pct, desired_reserve)
    ):
        raise ValueError("RSPS capital inputs must be finite numbers")
    if not 0 <= rsps_allocation_pct <= 1 or desired_reserve < 0:
        raise ValueError("RSPS allocation must be 0-1 and reserve non-negative")
    return max(0.0, total_equity * rsps_allocation_pct - desired_reserve)


def calculate_sdca_cash_balance(
    current_cash_usd: float, action: str, actual_quote_amount_usd: float
) -> float:
    """Apply a confirmed SDCA fill to its isolated USD pool."""
    if action not in {"buy", "sell"}:
        raise ValueError(f"Unsupported SDCA action: {action}")
    if actual_quote_amount_usd < 0:
        raise ValueError("Actual SDCA quote amount must not be negative")
    if action == "buy":
        return current_cash_usd - actual_quote_amount_usd
    return current_cash_usd + actual_quote_amount_usd


def generate_sdca_plan(action: str, sell_percentage: float | None = None) -> Dict:
    if action not in {"buy", "sell"}:
        raise ValueError(f"Unsupported SDCA action: {action}")

    sdca = get_sdca_sheet_values()
    exchange = get_kraken_exchange()
    symbol = get_best_trading_symbol("BTC")
    if _quote_currency(symbol) != "USD":
        raise ValueError(f"SDCA requires a BTC/USD market; selected {symbol}")
    ticker = exchange.fetch_ticker(symbol)
    price = ticker.get("last") or ticker.get("close")
    if not price or price <= 0:
        raise ValueError(f"No valid price available for {symbol}")

    if action == "buy":
        amount_usd = sdca["buy_pct"] * sdca["cash_usd"]
        amount_base = amount_usd / price
    else:
        if sell_percentage is None or not 0 < sell_percentage <= 100:
            raise ValueError(
                "SDCA sell percentage must be greater than 0 and at most 100"
            )
        amount_base = sdca["kraken_btc"] * sell_percentage / 100
        amount_usd = amount_base * price

    return {
        "action": action,
        "symbol": symbol,
        "price": float(price),
        "amount_usd": amount_usd,
        "amount_base": amount_base,
        "sell_percentage": sell_percentage,
        "cash_usd": sdca["cash_usd"],
        "kraken_btc": sdca["kraken_btc"],
        "cold_btc": sdca["cold_btc"],
        "dry_run": os.getenv("DRY_RUN", "false").lower() == "true",
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
    action: str, sell_percentage: float | None = None
) -> Dict[str, Any]:
    plan = generate_sdca_plan(action, sell_percentage)
    results: List[str] = []
    if plan["dry_run"]:
        results.append(
            f"🧪 DRY-RUN: Would {action} {plan['amount_base']:.8f} BTC "
            f"(~${plan['amount_usd']:.2f}); SDCA cash remains unchanged."
        )
        return {"results": results, "plan": plan, "dry_run": True}
    if plan["amount_base"] <= 0:
        raise ValueError(f"SDCA {action} amount is zero")
    cold_btc = get_btc_cold_balance()

    if action == "sell":
        free_btc = get_free_balance("BTC")
        if plan["amount_base"] > free_btc + 1e-8:
            raise ValueError(
                "SDCA sell exceeds free Kraken BTC; refusing to sell BTC "
                "outside the tracked SDCA balance."
            )
    else:
        free_usd = get_free_balance("USD")
        if plan["amount_usd"] > free_usd + 1e-8:
            raise ValueError(
                f"SDCA buy requires ${plan['amount_usd']:.2f}, but only "
                f"${free_usd:.2f} USD is available on Kraken."
            )

    exchange = get_kraken_exchange()
    ticker = exchange.fetch_ticker(plan["symbol"])
    valuation_price = ticker.get("last") or ticker.get("close")
    if not valuation_price or valuation_price <= 0:
        raise ValueError(f"No valid price available for {plan['symbol']}")
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

    initial_order = create_post_only_limit_order(
        plan["symbol"], side, amount_base, limit_price
    )
    orders = [initial_order]
    initial_status = str(initial_order.get("status", "")).lower()
    if (
        initial_order.get("id")
        and initial_status
        not in {"closed", "filled", "canceled", "cancelled", "expired"}
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
        }
        fallback_orders = _wait_for_order_completion(
            [order_reference],
            results,
            timeout_seconds=ORDER_TIMEOUT_SECONDS,
            poll_interval_seconds=ORDER_POLL_INTERVAL_SECONDS,
        )
        if fallback_orders:
            orders.extend(_cancel_and_market_fallback(fallback_orders, results))

    actual_quote_amount, btc_delta = _collect_sdca_order_fills(
        orders, plan["symbol"], action
    )
    if actual_quote_amount <= 0 or btc_delta == 0:
        raise RuntimeError("Kraken reported no completed SDCA BTC trade")

    sdca = get_sdca_sheet_values()
    cash_usd = calculate_sdca_cash_balance(
        sdca["cash_usd"], action, actual_quote_amount
    )
    kraken_btc = sdca["kraken_btc"] + btc_delta
    if kraken_btc < -1e-8:
        raise RuntimeError("SDCA sell would make its tracked BTC balance negative")
    kraken_btc = round(max(0.0, kraken_btc), 8)
    update_sdca_values(cash_usd, kraken_btc, cold_btc, float(valuation_price))

    results.append(
        f"✅ SDCA {action.upper()} filled: {abs(btc_delta):.8f} BTC, "
        f"${actual_quote_amount:.2f} {'spent' if action == 'buy' else 'proceeds'}."
    )
    if action == "buy":
        results.append(
            f"📤 Withdraw approximately {abs(btc_delta):.8f} BTC "
            "to cold storage when ready."
        )
    return {
        "results": results,
        "plan": plan,
        "actual_quote_amount_usd": actual_quote_amount,
        "btc_delta": btc_delta,
        "dry_run": False,
    }


def _submit_trade_orders(
    exchange, trades: List[Dict], dry_run: bool, results: List[str]
) -> List[Dict]:
    submitted_orders = []
    for trade in trades:
        try:
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
                }
            )
        except Exception as e:
            results.append(f"❌ Failed {trade['action']} {trade['asset']}: {e}")
    return submitted_orders


def _wait_for_order_completion(
    submitted_orders: List[Dict],
    results: List[str],
    timeout_seconds: int,
    poll_interval_seconds: int,
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

        remaining_orders = still_open_orders

        if not remaining_orders:
            return fallback_orders

        time.sleep(poll_interval_seconds)

    return fallback_orders + remaining_orders


def _cancel_and_market_fallback(orders: List[Dict], results: List[str]) -> List[Dict]:
    market_orders = []
    for order in orders:
        if not order.get("order_id"):
            continue

        order_info = fetch_order(order["order_id"], order["symbol"])
        remaining = (
            get_order_remaining(order_info) if order_info else order["amount_base"]
        )
        if remaining <= 0:
            continue

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
        except Exception as e:
            results.append(
                f"❌ Failed market fallback for {order['asset']} ({order['symbol']}): {e}"
            )
    return market_orders


def execute_trades(plan: List[Dict]):
    dry_run = os.getenv("DRY_RUN", "false").lower() == "true"
    exchange = get_kraken_exchange()
    results = []

    if not plan:
        return results
    reserved_sdca_cash = {"USD": get_sdca_sheet_values()["cash_usd"]}

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
        submitted_orders = _submit_trade_orders(exchange, trades, dry_run, results)
        if dry_run:
            return True
        orders_needing_fallback = _wait_for_order_completion(
            submitted_orders,
            results,
            timeout_seconds=ORDER_TIMEOUT_SECONDS,
            poll_interval_seconds=ORDER_POLL_INTERVAL_SECONDS,
        )
        if orders_needing_fallback:
            _cancel_and_market_fallback(orders_needing_fallback, results)
        return not is_rebalance_cancel_requested()

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
        return results
    if long_reductions and not execute_phase(long_reductions):
        results.append("⏹️ Rebalance cancelled after reducing spot longs.")
        return results

    if dry_run:
        remaining_plan = plan
    else:
        results.append(
            "🔄 Recalculating remaining targets from the post-reduction portfolio..."
        )
        try:
            remaining_plan = generate_rebalance_plan().get("plan", [])
        except Exception as e:
            results.append(f"❌ Failed to regenerate the remaining plan: {e}")
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
        return results
    if long_openings and not execute_phase(long_openings):
        results.append("⏹️ Rebalance cancelled after opening or increasing longs.")
        return results

    if is_rebalance_cancel_requested():
        results.append(
            "⏹️ Rebalance has been cancelled. No further trades will be placed."
        )

    return results
