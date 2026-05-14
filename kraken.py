import ccxt
import os
import time
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


def get_open_orders(symbols: List[str] | None = None) -> List[Dict]:
    """Fetch all open orders (or only for specific symbols)."""
    exchange = get_kraken_exchange()
    if not symbols:
        return exchange.fetch_open_orders()
    orders = []
    for symbol in symbols:
        try:
            orders.extend(exchange.fetch_open_orders(symbol))
        except Exception:
            pass
    return orders


def cancel_order(order_id: str) -> Dict:
    """Cancel a single order by ID."""
    exchange = get_kraken_exchange()
    return exchange.cancel_order(order_id)


def get_safe_post_only_price(
    ticker: Dict, side: str, buffer_pct: float = 0.0005
) -> float:
    """Return a limit price that is guaranteed to post (not cross the book)."""
    last = ticker.get("last") or ticker.get("close") or 0.0
    if side == "buy":
        ask = ticker.get("ask") or last
        return round(ask * (1 - buffer_pct), 8)  # slightly below ask → posts
    else:  # sell
        bid = ticker.get("bid") or last
        return round(bid * (1 + buffer_pct), 8)  # slightly above bid → posts


def create_post_only_limit_order(
    symbol: str, side: str, amount: float, price: float
) -> Dict:
    """
    Place a post-only limit order (maker fees).
    If the book shifted and it would cross, automatically fall back to market order.
    """
    exchange = get_kraken_exchange()
    params = {"post_only": True}  # CCXT translates to Kraken's oflags='post'

    try:
        order = exchange.create_order(symbol, "limit", side, amount, price, params)
        return order
    except Exception as e:
        err = str(e).lower()
        if any(
            kw in err
            for kw in [
                "post only",
                "would cross",
                "immediately match",
                "order rejected",
                "postonly",
            ]
        ):
            print(
                f"⚠️ Post-only {side} for {symbol} failed (book shift) → falling back to MARKET"
            )
            if side == "buy":
                return exchange.create_market_buy_order(symbol, amount)
            else:
                return exchange.create_market_sell_order(symbol, amount)
        # Any other error bubbles up
        raise
