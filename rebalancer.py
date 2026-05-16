import os
from typing import List, Dict, Any
from sheets import get_target_allocations, get_desired_cash_reserve
from kraken import (
    fetch_portfolio,
    get_kraken_exchange,
    get_open_orders,
    cancel_order,
    create_post_only_limit_order,
    get_safe_post_only_price,
    is_stable_coin,
    get_best_trading_symbol,
    validate_quote_balances,
)
from bot import CRYPTO_DECIMALS, PRICE_DECIMALS


def generate_rebalance_plan() -> Dict[str, Any]:
    exchange = get_kraken_exchange()
    current_portfolio, total_value, stable_breakdown = fetch_portfolio()
    targets = get_target_allocations()
    desired_reserve = get_desired_cash_reserve()

    investable_value = max(0.0, total_value - desired_reserve)
    plan: List[Dict] = []
    threshold = 5.0  # ignore tiny trades

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
                    "amount_usd": delta_usd,
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


def execute_trades(plan: List[Dict]):
    dry_run = os.getenv("DRY_RUN", "false").lower() == "true"
    exchange = get_kraken_exchange()
    results = []

    if not plan:
        return results

    # 1. Pre-check for conversion needs (after simulated sells)
    conversion_warnings = validate_quote_balances(plan)
    if conversion_warnings:
        results.extend(conversion_warnings)
        results.append(
            "⏸️  Trades paused until conversions are done. Run /rebalance again after converting."
        )
        return results

    # 2. ORDER MANAGEMENT: cancel any old orders that no longer matches the fresh plan
    if not dry_run:
        try:
            open_orders = get_open_orders([t["symbol"] for t in plan])
            for order in open_orders:
                try:
                    cancel_order(order["id"])
                    results.append(f"🧹 Cancelled old order {order['id']}")
                except Exception as ce:
                    results.append(f"⚠️ Failed to cancel {order['id']}: {ce}")
        except Exception as e:
            results.append(f"⚠️ Order cleanup failed: {e}")

    # 3. Execute: sells first, then buys
    sells = [t for t in plan if t["action"] == "sell"]
    buys = [t for t in plan if t["action"] == "buy"]
    for trade in sells + buys:
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
        except Exception as e:
            results.append(f"❌ Failed {trade['action']} {trade['asset']}: {e}")

    return results
