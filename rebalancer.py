import os
from typing import List, Dict, Any
from sheets import get_target_allocations
from kraken import (
    fetch_portfolio,
    get_kraken_exchange,
    get_open_orders,
    cancel_order,
    create_post_only_limit_order,
    get_safe_post_only_price,
)


def generate_rebalance_plan() -> Dict[str, Any]:
    quote = os.getenv("QUOTE_CURRENCY", "USDC").upper()
    exchange = get_kraken_exchange()
    current_portfolio, total_value = fetch_portfolio(quote)
    targets = get_target_allocations()

    plan: List[Dict] = []
    threshold = 5.0  # ignore tiny trades

    for asset, target_pct in targets.items():
        target_usd = target_pct * total_value
        current_usd = current_portfolio.get(asset, 0.0)
        delta_usd = target_usd - current_usd

        if abs(delta_usd) < threshold or asset == quote:
            continue

        try:
            symbol = f"{asset}/{quote}"
            ticker = exchange.fetch_ticker(symbol)
            price = ticker["last"]
            amount_base = abs(delta_usd / price)

            plan.append(
                {
                    "asset": asset,
                    "action": "buy" if delta_usd > 0 else "sell",
                    "amount_usd": round(delta_usd, 2),
                    "amount_base": round(amount_base, 8),
                    "price": round(price, 6),
                    "symbol": symbol,
                }
            )
        except Exception as e:
            print(f"❌ Could not create trade for {asset}: {e}")

    return {
        "total_value_usd": round(total_value, 2),
        "current_portfolio": {k: round(v, 2) for k, v in current_portfolio.items()},
        "plan": plan,
        "quote_currency": quote,
        "dry_run": os.getenv("DRY_RUN", "false").lower() == "true",
    }


def execute_trades(plan: List[Dict]):
    dry_run = os.getenv("DRY_RUN", "false").lower() == "true"
    exchange = get_kraken_exchange()
    results = []
    plan_symbols = [t["symbol"] for t in plan]

    # ORDER MANAGEMENT: cancel anything that no longer matches the fresh plan
    if not dry_run and plan:
        try:
            open_orders = get_open_orders(plan_symbols)
            for order in open_orders:
                try:
                    cancel_order(order["id"])
                    results.append(
                        f"🧹 Cancelled old order {order['id']} ({order.get('side')} {order.get('symbol')})"
                    )
                except Exception as ce:
                    results.append(f"⚠️ Failed to cancel {order['id']}: {ce}")
        except Exception as e:
            results.append(f"⚠️ Order cleanup failed: {e}")

    # EXECUTE THE NEW PLAN WITH POST-ONLY LIMITS
    for trade in plan:
        try:
            if dry_run:
                limit_price = trade["price"]  # just for display
                results.append(
                    f"🧪 DRY-RUN: Would {trade['action']} {trade['amount_base']} "
                    f"{trade['asset']} @ limit ~${limit_price} (POST-ONLY)"
                )
                continue

            # Fresh ticker -> safe post-only price
            ticker = exchange.fetch_ticker(trade["symbol"])
            limit_price = get_safe_post_only_price(ticker, trade["action"])

            order = create_post_only_limit_order(
                trade["symbol"],
                trade["action"],
                trade["amount_base"],
                limit_price,
            )

            results.append(
                f"✅ {trade['action'].upper()} POST-ONLY LIMIT {trade['amount_base']} "
                f"{trade['asset']} @ ~${limit_price} (ID: {order.get('id')})"
            )
        except Exception as e:
            results.append(f"❌ Failed {trade['action']} {trade['asset']}: {e}")

    return results
