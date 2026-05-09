import ccxt
import os
from typing import Dict, Tuple


def get_kraken_exchange():
    return ccxt.kraken(
        {
            "apiKey": os.getenv("KRAKEN_API_KEY"),
            "secret": os.getenv("KRAKEN_API_SECRET"),
            "enableRateLimit": True,
        }
    )


def fetch_portfolio(quote_currency: str = None) -> Tuple[Dict[str, float], float]:
    """Returns {asset: value_in_quote}, total_value_in_quote"""
    if quote_currency is None:
        quote_currency = os.getenv("QUOTE_CURRENCY", "USDC").upper()

    exchange = get_kraken_exchange()
    balance = exchange.fetch_balance()["free"]

    portfolio: Dict[str, float] = {}
    total_value = 0.0

    for currency, amount in balance.items():
        if amount <= 0:
            continue
        currency = currency.upper()

        if currency == quote_currency:
            portfolio[currency] = amount
            total_value += amount
            continue

        # Get market value
        try:
            symbol = f"{currency}/{quote_currency}"
            ticker = exchange.fetch_ticker(symbol)
            price = ticker.get("last") or ticker.get("close")
            if price is None:
                continue
            value = amount * price
            portfolio[currency] = value
            total_value += value
        except Exception:
            pass  # skip illiquid pairs

    return portfolio, total_value
