import ccxt
import os
from typing import Dict, List, Tuple, Any

_exchange = None

def get_kraken_exchange():
    """Singleton CCXT Kraken exchange instance for reuse."""
    global _exchange
    if _exchange is None:
        _exchange = ccxt.kraken(
            {
                "apiKey": os.getenv("KRAKEN_API_KEY"),
                "secret": os.getenv("KRAKEN_API_SECRET"),
                "enableRateLimit": True,
            }
        )
    return _exchange


def fetch_tickers_batch(symbols: List[str]) -> Dict[str, float]:
    """Fetch multiple tickers in one API call. Returns {symbol: last_price}."""
    if not symbols:
        return {}
    exchange = get_kraken_exchange()
    prices = {}
    try:
        tickers = exchange.fetch_tickers(symbols)
        for symbol, ticker in tickers.items():
            price = ticker.get("last") or ticker.get("close")
            if price is not None:
                prices[symbol] = price
    except Exception as e:
        print(f"⚠️ Batch ticker fetch failed: {e}. Falling back to individual fetches.")
        # Fallback
        for symbol in symbols:
            try:
                ticker = exchange.fetch_ticker(symbol)
                price = ticker.get("last") or ticker.get("close")
                if price is not None:
                    prices[symbol] = price
            except Exception:
                pass
    return prices


def fetch_portfolio(quote_currency: str = None) -> Tuple[Dict[str, float], float]:
    """Returns {asset: value_in_quote}, total_value_in_quote
    Now uses batch ticker fetching for speed."""
    if quote_currency is None:
        quote_currency = os.getenv("QUOTE_CURRENCY", "USDC").upper()

    exchange = get_kraken_exchange()
    balance = exchange.fetch_balance()["free"]

    # Collect symbols needed for valuation
    needed_symbols = []
    for currency, amount in balance.items():
        if amount > 0:
            currency_upper = currency.upper()
            if currency_upper != quote_currency:
                symbol = f"{currency_upper}/{quote_currency}"
                needed_symbols.append(symbol)

    prices = fetch_tickers_batch(needed_symbols)

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

        symbol = f"{currency}/{quote_currency}"
        price = prices.get(symbol)
        if price is None:
            continue
        value = amount * price
        portfolio[currency] = value
        total_value += value

    return portfolio, total_value
