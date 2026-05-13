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

def get_stable_balances() -> Dict[str, float]:
    """Returns current free USD and USDC balances."""
    exchange = get_kraken_exchange()
    balance = exchange.fetch_balance()["free"]
    return {
        "USD": float(balance.get("USD", 0.0)),
        "USDC": float(balance.get("USDC", 0.0)),
    }

def fetch_portfolio(quote_currency: str = None) -> Tuple[Dict[str, float], float]:
    """Updated to handle both USD and USDC. Values all in USD equivalent. Stables valued 1:1."""
    if quote_currency is None:
        quote_currency = "USD"  # prefer USD for valuation

    exchange = get_kraken_exchange()
    balance = exchange.fetch_balance()["free"]

    # Collect symbols needed for valuation (both USD and USDC pairs)
    needed_symbols = []
    for currency, amount in balance.items():
        if amount > 0:
            currency_upper = currency.upper()
            if currency_upper not in ["USD", "USDC"]:
                needed_symbols.append(f"{currency_upper}/USD")
                needed_symbols.append(f"{currency_upper}/USDC")

    prices = fetch_tickers_batch(needed_symbols)

    portfolio: Dict[str, float] = {}
    total_value = 0.0

    # Handle stables separately
    usd = float(balance.get("USD", 0.0))
    usdc = float(balance.get("USDC", 0.0))
    portfolio["USD"] = usd
    portfolio["USDC"] = usdc
    total_value += usd + usdc

    for currency, amount in balance.items():
        if amount <= 0:
            continue
        currency = currency.upper()
        if currency in ["USD", "USDC"]:
            continue  # already handled

        # Try USD pair first, fallback to USDC
        symbol = f"{currency}/USD"
        price = prices.get(symbol)
        if price is None:
            symbol = f"{currency}/USDC"
            price = prices.get(symbol)
        if price is None:
            continue
        value = amount * price
        portfolio[currency] = round(value, 2)
        total_value += value

    return portfolio, round(total_value, 2)

def get_open_orders(symbol: str = None) -> List[Dict]:
    """Fetch all open orders or for specific symbol."""
    exchange = get_kraken_exchange()
    try:
        return exchange.fetch_open_orders(symbol=symbol)
    except Exception as e:
        print(f"❌ Error fetching open orders: {e}")
        return []

def cancel_order(order_id: str, symbol: str = None) -> Dict:
    """Cancel specific order."""
    exchange = get_kraken_exchange()
    try:
        return exchange.cancel_order(order_id, symbol)
    except Exception as e:
        print(f"❌ Failed to cancel order {order_id}: {e}")
        raise

def cancel_all_open_orders(symbol: str = None) -> List:
    """Cancel all open orders (batch where possible)."""
    exchange = get_kraken_exchange()
    try:
        if symbol:
            return exchange.cancel_all_orders(symbol)
        else:
            # Kraken may not support global without symbol, fallback to individual
            orders = get_open_orders()
            cancelled = []
            for order in orders:
                try:
                    cancelled.append(cancel_order(order['id'], order.get('symbol')))
                except:
                    pass
            return cancelled
    except Exception as e:
        print(f"❌ Error cancelling all orders: {e}")
        return []

def create_post_only_limit_order(symbol: str, side: str, amount: float, price: float, params: Dict = None) -> Dict:
    """Create post-only limit order to ensure maker fees only."""
    if params is None:
        params = {}
    # Kraken specific: post-only flag
    params["oflags"] = "post"
    exchange = get_kraken_exchange()
    try:
        order = exchange.create_order(
            symbol, "limit", side, amount, price, params
        )
        return order
    except Exception as e:
        print(f"❌ Failed to create post-only {side} order for {symbol}: {e}")
        raise

def edit_order(order_id: str, symbol: str, side: str, amount: float = None, price: float = None, params: Dict = None) -> Dict:
    """Edit an existing order (keep post-only)."""
    if params is None:
        params = {}
    params["oflags"] = "post"
    exchange = get_kraken_exchange()
    try:
        order = exchange.edit_order(order_id, symbol, "limit", side, amount, price, params)
        return order
    except Exception as e:
        print(f"❌ Failed to edit order {order_id}: {e}")
        raise

def fetch_order(order_id: str) -> Dict:
    """Fetch status of a specific order."""
    exchange = get_kraken_exchange()
    try:
        return exchange.fetch_order(order_id)
    except Exception as e:
        print(f"❌ Error fetching order {order_id}: {e}")
        raise
