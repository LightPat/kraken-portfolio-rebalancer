import os
from typing import List, Dict, Any
from sheets import get_target_allocations
from kraken import fetch_portfolio, get_kraken_exchange


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

    for trade in plan:
        try:
            if dry_run:
                results.append(
                    f"🧪 DRY-RUN: Would {trade['action']} {trade['amount_base']} "
                    f"{trade['asset']} @ ~${trade['price']}"
                )
                continue

            if trade["action"] == "buy":
                order = exchange.create_market_buy_order(
                    trade["symbol"], trade["amount_base"]
                )
            else:
                order = exchange.create_market_sell_order(
                    trade["symbol"], trade["amount_base"]
                )

            results.append(
                f"✅ {trade['action'].upper()} {trade['amount_base']} {trade['asset']} executed (ID: {order.get('id')})"
            )
        except Exception as e:
            results.append(f"❌ Failed {trade['action']} {trade['asset']}: {e}")

    return results
