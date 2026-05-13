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
    """Returns {asset: value_in_quote}, total_value_in_quote"""
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

# New functions for order management

def fetch_open_orders(symbol: str = None) -> List[Dict]:
    """Fetch open orders, optionally for a specific symbol."""
    exchange = get_kraken_exchange()
    return exchange.fetch_open_orders(symbol)

def cancel_order(order_id: str, symbol: str = None) -> Dict:
    """Cancel a specific order."""
    exchange = get_kraken_exchange()
    return exchange.cancel_order(order_id, symbol)

def cancel_all_orders(symbol: str = None) -> List[Dict]:
    """Cancel all open orders, optionally filtered by symbol."""
    exchange = get_kraken_exchange()
    return exchange.cancel_all_orders(symbol)

def fetch_order_book(symbol: str, limit: int = 10) -> Dict:
    """Fetch order book for price discovery."""
    exchange = get_kraken_exchange()
    return exchange.fetch_order_book(symbol, limit)

def create_post_only_limit_order(symbol: str, side: str, amount: float, price: float, params: Dict = None) -> Dict:
    """Create a post-only limit order to ensure maker fees."""
    if params is None:
        params = {}
    exchange = get_kraken_exchange()
    # Kraken specific for post-only
    if 'kraken' in str(exchange.id).lower():
        params.setdefault('oflags', 'post')
    else:
        params.setdefault('post_only', True)
    
    order = exchange.create_order(
        symbol, 'limit', side, amount, price, params=params
    )
    return order
