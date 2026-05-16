import ccxt
import os
from typing import Dict, List, Tuple, Set

QUOTE_CURRENCY = "USD"
STABLE_COINS: Set[str] = {"USD", "USDC", "USDG"}

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


def is_stable_coin(currency: str) -> bool:
    return currency.upper() in STABLE_COINS


def get_best_trading_symbol(asset: str) -> str:
    """Prefer USD pair (higher volume). Fallback to other stables if pair doesn't exist."""
    exchange = get_kraken_exchange()
    # Ensure markets are loaded so we can safely inspect them
    if not getattr(exchange, "markets", None):
        try:
            exchange.load_markets()
        except Exception as e:
            print(f"⚠️ Failed to load markets from exchange: {e}")

    markets = getattr(exchange, "markets", {})
    for quote in [QUOTE_CURRENCY] + [q for q in STABLE_COINS if q != QUOTE_CURRENCY]:
        symbol = f"{asset.upper()}/{quote}"
        # Use the markets mapping (dict) rather than testing membership on the exchange object
        if symbol in markets and markets[symbol].get("active", False):
            return symbol
    raise ValueError(f"No active trading pair found for {asset} against any stablecoin")


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


def fetch_portfolio() -> Tuple[Dict[str, float], float, Dict[str, float]]:
    """Returns {asset: value_in_usd}, total_value_in_usd, {stable_coin: amount}"""
    exchange = get_kraken_exchange()
    # This is the dictionary of ASSET: CURRENT BALANCE
    # For example: 'BNB': 8.62915588
    balance = exchange.fetch_balance()["free"]

    # Collect symbols needed for valuation
    needed_symbols = []
    asset_to_symbol = {}
    for currency, amount in balance.items():
        if amount > 0 and not is_stable_coin(currency.upper()):
            try:
                symbol = get_best_trading_symbol(currency.upper())
                needed_symbols.append(symbol)
                asset_to_symbol[currency.upper()] = symbol
            except Exception as e:
                print(f"⚠️ Skipping {currency} - no trading pair found: {e}")

    # This is the dictionary of ASSET PAIR: PRICE
    # For example: 'BNB/USD': 652.02
    prices = fetch_tickers_batch(needed_symbols)

    portfolio: Dict[str, float] = {}
    total_value = 0.0
    stable_breakdown: Dict[str, float] = {s: 0.0 for s in STABLE_COINS}

    for currency, amount in balance.items():
        if amount <= 0:
            continue
        currency = currency.upper()

        if is_stable_coin(currency):
            # Stables valued at par due to fee free conversions in the UI
            stable_breakdown[currency] = amount
            total_value += amount
        else:  # Non-stable asset
            symbol = asset_to_symbol.get(currency)
            if not symbol:
                continue
            price = prices.get(symbol)
            if price is None:
                continue
            value = amount * price
            portfolio[currency] = value
            total_value += value

    return portfolio, total_value, stable_breakdown


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


def get_free_balance(currency: str) -> float:
    """Quick free balance lookup (used during execution)."""
    exchange = get_kraken_exchange()
    balance = exchange.fetch_balance()["free"]
    return balance.get(currency.upper(), 0.0)


def validate_quote_balances(plan: List[Dict]) -> List[str]:
    """Check if we have enough quote currency for buys. Return warning messages."""
    warnings = []
    # Start with current free balances for stables only
    exchange = get_kraken_exchange()
    balance = exchange.fetch_balance()["free"]
    projected = {s: balance.get(s, 0.0) for s in STABLE_COINS}

    # 1. Simulate sells (add proceeds)
    for trade in [t for t in plan if t["action"] == "sell"]:
        quote = trade["quote"]
        if quote in projected:
            projected[quote] += trade[
                "amount_usd"
            ]  # approximate; real fill will be close

    # 2. Check buys against projected balances
    for trade in [t for t in plan if t["action"] == "buy"]:
        quote = trade["quote"]
        needed = trade["amount_usd"]
        available = projected.get(quote, 0.0)
        if available < needed:
            shortfall = needed - available
            # Suggest conversion from any other stable we hold
            other_stables = [s for s in STABLE_COINS if s != quote]
            warnings.append(
                f"⚠️ CONVERSION NEEDED: Buy {trade['asset']}/{quote} requires ${shortfall:,.2f} more {quote}. "
                f"You have sufficient total stables but not enough in {quote} after sells. "
                f"Please use Kraken Pro → Convert to move ~${shortfall:,.2f} from {other_stables[0]} (or any other stable) → {quote}."
            )
    return warnings
