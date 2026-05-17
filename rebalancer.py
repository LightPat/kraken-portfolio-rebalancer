import os
import time
from typing import List, Dict, Any
from sheets import get_target_allocations, get_desired_cash_reserve
from kraken import (
    fetch_portfolio,
    get_kraken_exchange,
    get_open_orders,
    cancel_order,
    cancel_open_orders,
    create_post_only_limit_order,
    get_safe_post_only_price,
    is_stable_coin,
    get_best_trading_symbol,
    validate_quote_balances,
    fetch_order,
    get_order_remaining,
)
from bot import CRYPTO_DECIMALS, PRICE_DECIMALS

ORDER_TIMEOUT_SECONDS = 300
ORDER_POLL_INTERVAL_SECONDS = 3

CANCEL_REBALANCE_REQUESTED = False


def request_cancel_rebalance():
    global CANCEL_REBALANCE_REQUESTED
    CANCEL_REBALANCE_REQUESTED = True


def reset_cancel_rebalance():
    global CANCEL_REBALANCE_REQUESTED
    CANCEL_REBALANCE_REQUESTED = False


def is_rebalance_cancel_requested() -> bool:
    return CANCEL_REBALANCE_REQUESTED


def generate_rebalance_plan() -> Dict[str, Any]:
    exchange = get_kraken_exchange()
    current_portfolio, total_value, stable_breakdown = fetch_portfolio()
    targets = get_target_allocations()
    desired_reserve = get_desired_cash_reserve()

    investable_value = max(0.0, total_value - desired_reserve)
    plan: List[Dict] = []
    threshold = 15  # ignore tiny trades

    for asset, target_pct in targets.items():
        target_usd = target_pct * investable_value
        current_usd = current_portfolio.get(asset, 0.0)
        delta_usd = target_usd - current_usd

        if abs(delta_usd) < threshold or is_stable_coin(asset):
            continue

        try:
            symbol = get_best_trading_symbol(asset)
            ticker = exchange.fetch_ticker(symbol)
            price = ticker["last"]
            amount_base = abs(delta_usd / price)

            plan.append(
                {
                    "asset": asset,
                    "action": "buy" if delta_usd > 0 else "sell",
                    "amount_usd": abs(delta_usd),
                    "amount_base": amount_base,
                    "price": price,
                    "symbol": symbol,
                    "quote": symbol.split("/")[1],
                }
            )
        except Exception as e:
            print(f"❌ Could not create trade for {asset}: {e}")

    current_stables_total = sum(stable_breakdown.values())
    return {
        "total_value_usd": total_value,
        "desired_reserve": desired_reserve,
        "investable_value": investable_value,
        "current_stables_total": current_stables_total,
        "current_portfolio": {k: v for k, v in current_portfolio.items()},
        "plan": plan,
        "dry_run": os.getenv("DRY_RUN", "false").lower() == "true",
    }


def _submit_trade_orders(
    exchange, trades: List[Dict], dry_run: bool, results: List[str]
) -> List[Dict]:
    submitted_orders = []
    for trade in trades:
        try:
            ticker = exchange.fetch_ticker(trade["symbol"])
            limit_price = get_safe_post_only_price(ticker, trade["action"])

            if dry_run:
                results.append(
                    f"🧪 DRY-RUN: Would {trade['action']} {round(trade['amount_base'], CRYPTO_DECIMALS)} {trade['asset']} @ ~${round(limit_price, PRICE_DECIMALS)}"
                )
                continue

            order = create_post_only_limit_order(
                trade["symbol"], trade["action"], trade["amount_base"], limit_price
            )
            results.append(
                f"✅ {trade['action'].upper()} {round(trade['amount_base'], CRYPTO_DECIMALS)} {trade['asset']} @ LIMIT ~${round(limit_price, PRICE_DECIMALS)} (ID: {order.get('id')})"
            )

            submitted_orders.append(
                {
                    "order_id": order.get("id"),
                    "symbol": trade["symbol"],
                    "side": trade["action"],
                    "amount_base": trade["amount_base"],
                    "quote": trade["quote"],
                    "asset": trade["asset"],
                }
            )
        except Exception as e:
            results.append(f"❌ Failed {trade['action']} {trade['asset']}: {e}")
    return submitted_orders


def _wait_for_order_completion(
    exchange,
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
    while time.monotonic() - start_time < timeout_seconds:
        if is_rebalance_cancel_requested():
            results.append(
                "⏹️ Rebalance cancellation requested, stopping order monitoring."
            )
            return remaining_orders

        open_orders = get_open_orders(symbols)
        open_ids = {order["id"] for order in open_orders}
        remaining_orders = [
            order for order in remaining_orders if order["order_id"] in open_ids
        ]

        if not remaining_orders:
            return []

        time.sleep(poll_interval_seconds)

    return remaining_orders


def _cancel_and_market_fallback(exchange, orders: List[Dict], results: List[str]):
    for order in orders:
        if not order.get("order_id"):
            continue

        order_info = fetch_order(order["order_id"], order["symbol"]) or {}
        remaining = get_order_remaining(order_info)
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
            if order["side"] == "buy":
                market = exchange.create_market_buy_order(order["symbol"], remaining)
            else:
                market = exchange.create_market_sell_order(order["symbol"], remaining)
            results.append(
                f"🚀 MARKET {order['side'].upper()} for {order['asset']} remaining {round(remaining, CRYPTO_DECIMALS)} @ {order['symbol']} (ID: {market.get('id')})"
            )
        except Exception as e:
            results.append(
                f"❌ Failed market fallback for {order['asset']} ({order['symbol']}): {e}"
            )


def execute_trades(plan: List[Dict]):
    dry_run = os.getenv("DRY_RUN", "false").lower() == "true"
    exchange = get_kraken_exchange()
    results = []

    if not plan:
        return results

    conversion_warnings = validate_quote_balances(plan)
    if conversion_warnings:
        results.extend(conversion_warnings)
        results.append(
            "⏸️ Trades paused until conversions are done. Run /rebalance again after converting."
        )
        return results

    if not dry_run:
        try:
            cancelled = cancel_open_orders([t["symbol"] for t in plan])
            for order_id in cancelled:
                results.append(f"🧹 Cancelled existing order {order_id}")
        except Exception as e:
            results.append(f"⚠️ Order cleanup failed: {e}")

    sells = [t for t in plan if t["action"] == "sell"]
    buys = [t for t in plan if t["action"] == "buy"]

    sell_orders = _submit_trade_orders(exchange, sells, dry_run, results)
    if not dry_run:
        timed_out_sells = _wait_for_order_completion(
            exchange,
            sell_orders,
            results,
            timeout_seconds=ORDER_TIMEOUT_SECONDS,
            poll_interval_seconds=ORDER_POLL_INTERVAL_SECONDS,
        )
        if timed_out_sells:
            _cancel_and_market_fallback(exchange, timed_out_sells, results)

        # Re-calculate buys with actual post-sell balances
        results.append(
            "🔄 Re-calculating buy orders using actual post-sell portfolio..."
        )
        try:
            fresh_rebalance = generate_rebalance_plan()
            new_plan = fresh_rebalance.get("plan", [])

            # Exclude any assets we just sold in this cycle
            # (prevents immediate buy-backs due to tiny price drift)
            sold_assets = {t.get("asset") for t in sells if t.get("asset")}

            buys = [
                t
                for t in new_plan
                if t.get("action") == "buy" and t.get("asset") not in sold_assets
            ]

            if buys:
                results.append(
                    f"📊 Recalculated {len(buys)} buy order(s) based on current balances and prices."
                )
            else:
                results.append("✅ No buy orders needed after re-calculation.")
        except Exception as e:
            results.append(
                f"⚠️ Failed to re-generate plan: {e}. Falling back to original buys."
            )

    if not dry_run:
        # Use the fresh plan (or the original if re-calc failed) for the final validation
        buy_warnings = validate_quote_balances(
            new_plan if "new_plan" in locals() else plan
        )
        if buy_warnings:
            results.extend(buy_warnings)
            results.append(
                "⏸️ Aborting buy execution until stablecoin conversions are completed."
            )
            return results

    buy_orders = _submit_trade_orders(exchange, buys, dry_run, results)
    if not dry_run:
        timed_out_buys = _wait_for_order_completion(
            exchange,
            buy_orders,
            results,
            timeout_seconds=ORDER_TIMEOUT_SECONDS,
            poll_interval_seconds=ORDER_POLL_INTERVAL_SECONDS,
        )
        if timed_out_buys:
            _cancel_and_market_fallback(exchange, timed_out_buys, results)

    if is_rebalance_cancel_requested():
        results.append(
            "⏹️ Rebalance has been cancelled. No further trades will be placed."
        )

    return results
