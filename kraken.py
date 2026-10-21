import ccxt
import os
import time
from datetime import datetime
from pathlib import Path
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


def fetch_portfolio() -> Tuple[
    Dict[str, float],
    Dict[str, float],
    Dict[str, float],
    Dict[str, float],
    float,
    Dict[str, float],
]:
    """Return spot and margin values, account equity, and free stable balances."""
    exchange = get_kraken_exchange()
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

    spot_value_by_asset: Dict[str, float] = {}
    stable_breakdown: Dict[str, float] = {s: 0.0 for s in STABLE_COINS}
    total_equity = 0.0

    for currency, amount in balance.items():
        if amount <= 0:
            continue
        currency = currency.upper()

        if is_stable_coin(currency):
            stable_breakdown[currency] = amount
            total_equity += amount
        else:  # Non-stable asset
            symbol = asset_to_symbol.get(currency)
            if not symbol:
                continue
            price = prices.get(symbol)
            if price is None:
                continue
            value = amount * price
            spot_value_by_asset[currency] = value
            total_equity += value

    positions = fetch_margin_positions()
    short_notional_by_asset: Dict[str, float] = {}
    short_used_margin_by_asset: Dict[str, float] = {}
    long_margin_notional_by_asset: Dict[str, float] = {}
    for position in positions:
        asset = position["asset"]
        if position["side"] == "short":
            short_notional_by_asset[asset] = (
                short_notional_by_asset.get(asset, 0.0) + position["notional_usd"]
            )
            short_used_margin_by_asset[asset] = (
                short_used_margin_by_asset.get(asset, 0.0)
                + position["used_margin_usd"]
            )
        else:
            long_margin_notional_by_asset[asset] = (
                long_margin_notional_by_asset.get(asset, 0.0)
                + position["notional_usd"]
            )
        total_equity += position["used_margin_usd"] + position["unrealized_pnl_usd"]

    return (
        spot_value_by_asset,
        short_notional_by_asset,
        short_used_margin_by_asset,
        long_margin_notional_by_asset,
        max(0.0, total_equity),
        stable_breakdown,
    )


def fetch_margin_positions() -> List[Dict]:
    """Return open Kraken spot-margin positions in normalized USD terms."""
    exchange = get_kraken_exchange()
    try:
        positions = exchange.fetch_positions()
    except (ccxt.NotSupported, AttributeError):
        try:
            exchange.load_markets()
            response = exchange.privatePostOpenPositions(
                {"docalcs": "true", "consolidation": "market"}
            )
        except Exception as e:
            raise RuntimeError(
                "Failed to fetch Kraken spot-margin positions. Confirm the API key "
                f"has account permissions and margin trading is enabled: {e}"
            ) from e
        raw_positions = response.get("result", [])
        if isinstance(raw_positions, dict):
            raw_positions = list(raw_positions.values())
        positions = []
        for raw_position in raw_positions:
            symbol = exchange.safe_symbol(raw_position.get("pair"))
            size_base = float(raw_position.get("vol") or 0.0)
            entry_price = (
                float(raw_position.get("cost") or 0.0) / size_base
                if size_base
                else 0.0
            )
            raw_side = raw_position.get("type")
            if raw_side not in {"buy", "sell"}:
                raise ValueError(
                    f"Unexpected Kraken margin position side: {raw_side!r}"
                )
            side = "long" if raw_side == "buy" else "short"
            positions.append(
                {
                    "info": raw_position,
                    "symbol": symbol,
                    "side": side,
                    "contracts": size_base,
                    "entryPrice": entry_price,
                    "initialMargin": float(raw_position.get("margin") or 0.0),
                    "unrealizedPnl": float(raw_position.get("net") or 0.0),
                }
            )
    except Exception as e:
        raise RuntimeError(
            "Failed to fetch Kraken spot-margin positions. Confirm the API key has "
            f"the required account permissions and margin trading is enabled: {e}"
        ) from e

    position_details = []
    symbols = []
    for position in positions:
        info = position.get("info") or {}
        size_base = float(position.get("contracts") or info.get("vol") or 0.0)
        if size_base <= 0:
            continue

        symbol = position.get("symbol")
        side = position.get("side")
        if not symbol or side not in {"long", "short"}:
            raise ValueError(f"Unexpected Kraken margin position: {position!r}")
        entry_price = float(
            position.get("entryPrice")
            or (
                float(info.get("cost") or 0.0) / size_base
                if size_base
                else 0.0
            )
        )
        used_margin = float(
            position.get("initialMargin") or info.get("margin") or 0.0
        )
        if used_margin <= 0:
            raise RuntimeError(
                f"Kraken did not return used margin for open position {symbol}"
            )
        position_details.append(
            {
                "asset": symbol.split("/")[0].upper(),
                "symbol": symbol,
                "side": side,
                "size_base": size_base,
                "entry_price": entry_price,
                "used_margin_usd": used_margin,
                "unrealized_pnl_usd": float(
                    position.get("unrealizedPnl")
                    if position.get("unrealizedPnl") is not None
                    else info.get("net") or 0.0
                ),
            }
        )
        symbols.append(symbol)

    prices = fetch_tickers_batch(list(dict.fromkeys(symbols)))
    for position in position_details:
        price = prices.get(position["symbol"])
        if price is None or price <= 0:
            raise RuntimeError(
                f"Failed to fetch a valid price for open margin position "
                f"{position['symbol']}"
            )
        position["mark_price"] = price
        position["notional_usd"] = position["size_base"] * price
        if position["entry_price"] <= 0:
            raise RuntimeError(
                f"Kraken did not return an entry price for margin position "
                f"{position['symbol']}"
            )
        if position["unrealized_pnl_usd"] == 0.0:
            sign = 1 if position["side"] == "long" else -1
            position["unrealized_pnl_usd"] = (
                sign
                * (position["mark_price"] - position["entry_price"])
                * position["size_base"]
            )
    return position_details


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


def _margin_order_error(error: Exception) -> RuntimeError:
    error_text = str(error).lower()
    if isinstance(error, ccxt.InsufficientFunds) or any(
        phrase in error_text
        for phrase in ("insufficient funds", "insufficient margin", "not enough")
    ):
        reason = "Insufficient quote funds or margin collateral."
    elif any(
        phrase in error_text
        for phrase in (
            "permission",
            "not permitted",
            "margin disabled",
            "margin is disabled",
            "not enabled",
        )
    ):
        reason = "Margin trading is disabled or the API key lacks margin permission."
    else:
        reason = "Kraken rejected the spot-margin order."
    return RuntimeError(
        f"{reason} Spot-margin leverage is fixed at 2x. "
        f"Kraken returned: {error}"
    )


def _force_2x_leverage(params: Dict) -> None:
    requested_leverage = params.get("leverage", 2)
    try:
        is_2x = float(requested_leverage) == 2
    except (TypeError, ValueError):
        is_2x = False
    if not is_2x:
        print(
            f"⚠️ Refusing spot-margin leverage {requested_leverage!r}; "
            "clamping to Kraken minimum 2x."
        )
    params["leverage"] = "2"


def create_market_order(
    symbol: str,
    side: str,
    amount: float,
    margin: bool = False,
    reduce_only: bool = False,
) -> Dict:
    """Place a market order, optionally as a 2x spot-margin order."""
    exchange = get_kraken_exchange()
    params = {}
    if margin:
        _force_2x_leverage(params)
    if reduce_only:
        params["reduceOnly"] = True

    try:
        return exchange.create_order(symbol, "market", side, amount, None, params)
    except Exception as e:
        if margin:
            raise _margin_order_error(e) from e
        raise


def create_post_only_limit_order(
    symbol: str,
    side: str,
    amount: float,
    price: float,
    margin: bool = False,
    reduce_only: bool = False,
) -> Dict:
    """
    Place a post-only limit order (maker fees).
    If the book shifted and it would cross, automatically fall back to market order.
    """
    exchange = get_kraken_exchange()
    params = {"postOnly": True}
    if margin:
        _force_2x_leverage(params)
    if reduce_only:
        params["reduceOnly"] = True

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
                "postonly",
            ]
        ):
            print(
                f"⚠️ Post-only {side} for {symbol} failed (book shift) → falling back to MARKET"
            )
            return create_market_order(
                symbol, side, amount, margin=margin, reduce_only=reduce_only
            )
        # Any other error bubbles up
        if margin:
            raise _margin_order_error(e) from e
        raise


def get_free_balance(currency: str) -> float:
    """Quick free balance lookup (used during execution)."""
    exchange = get_kraken_exchange()
    balance = exchange.fetch_balance()["free"]
    return balance.get(currency.upper(), 0.0)


def get_order_remaining(order: Dict) -> float:
    """Return the remaining base amount for an order, even if CCXT doesn't expose `remaining`."""
    remaining = order.get("remaining")
    if remaining is None:
        amount = float(order.get("amount", 0.0) or 0.0)
        filled = float(order.get("filled", 0.0) or 0.0)
        remaining = max(0.0, amount - filled)
    return float(remaining)


def fetch_order(order_id: str, symbol: str) -> Dict | None:
    """Refresh a single order by ID and symbol."""
    exchange = get_kraken_exchange()
    try:
        return exchange.fetch_order(order_id, symbol)
    except Exception as e:
        print(f"⚠️ Failed to refresh order {order_id}: {e}")
        return None


def cancel_open_orders(symbols: List[str] | None = None) -> List[str]:
    """Cancel all open orders, optionally filtered by a list of symbols."""
    cancelled = []
    orders = get_open_orders(symbols)
    for order in orders:
        try:
            cancel_order(order["id"])
            cancelled.append(order["id"])
        except Exception as e:
            print(f"⚠️ Failed to cancel open order {order.get('id')}: {e}")
    return cancelled


def validate_quote_balances(plan: List[Dict]) -> List[str]:
    """Check available quote funds for short collateral and spot buys."""
    warnings = []
    # Start with current free balances for stables only
    exchange = get_kraken_exchange()
    balance = exchange.fetch_balance()["free"]
    projected = {s: balance.get(s, 0.0) for s in STABLE_COINS}

    for position in fetch_margin_positions():
        quote = position["symbol"].split("/")[1].upper()
        if position["side"] == "short" and quote in projected:
            short_proceeds = position["entry_price"] * position["size_base"]
            projected[quote] = max(0.0, projected[quote] - short_proceeds)

    # Only spot long reductions add quote funds. Margin short sale proceeds are
    # collateral for that short and are never available to fund another action.
    for trade in plan:
        if trade["action"] in {"reduce_long", "close_long", "sell"} and not trade.get(
            "margin", False
        ):
            quote = trade["quote"]
            if quote in projected:
                projected[quote] += trade["amount_usd"]

    for trade in plan:
        quote = trade["quote"]
        if trade["action"] in {"cover_short", "reduce_short"}:
            needed = trade["amount_usd"]
        elif trade["action"] in {"open_short", "increase_short"}:
            needed = trade["capital_usd"]
        elif trade["action"] in {"open_long", "increase_long", "buy"}:
            needed = trade["amount_usd"]
        else:
            continue

        available = projected.get(quote, 0.0)
        if available < needed:
            shortfall = needed - available
            warnings.append(
                f"⚠️ CONVERSION NEEDED: {trade['action'].replace('_', ' ')} "
                f"{trade['asset']}/{quote} requires ${shortfall:,.2f} more {quote}. "
                f"Only ${available:,.2f} {quote} is available after reserving "
                "existing short proceeds and required margin capital. Fund the "
                "quote balance or convert from another stablecoin if needed."
            )
        else:
            projected[quote] -= needed
    return warnings
