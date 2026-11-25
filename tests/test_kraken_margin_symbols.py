import unittest
from unittest.mock import Mock, patch

import kraken


class BitnomialMarketTests(unittest.TestCase):
    def setUp(self):
        self.exchange = Mock()
        self.exchange.markets = {}
        self.exchange.markets_by_id = {}
        self.exchange.symbols = []
        self.exchange.safe_currency_code.side_effect = {
            "XETH": "ETH",
            "ZUSD": "USD",
        }.__getitem__

    def test_registers_bitnomial_markets_idempotently(self):
        raw_market = {
            "wsname": "ETH/USD:BTNL",
            "altname": "ETHUSD:BTNL",
            "base": "XETH",
            "quote": "ZUSD",
            "lot_decimals": "8",
            "tick_size": "0.01",
        }
        self.exchange.publicGetAssetPairs.return_value = {
            "result": {"ETHUSD:BTNL": raw_market}
        }

        kraken._ensure_bitnomial_markets(self.exchange)
        kraken._ensure_bitnomial_markets(self.exchange)

        market = self.exchange.markets["ETH/USD:BTNL"]
        self.assertEqual(market["id"], "ETHUSD:BTNL")
        self.assertEqual((market["base"], market["quote"]), ("ETH", "USD"))
        self.assertTrue(market["active"])
        self.assertEqual(market["type"], "spot")
        self.assertTrue(market["spot"])
        self.assertTrue(market["margin"])
        self.assertEqual(market["precision"], {"amount": 1e-8, "price": 0.01})
        self.assertIs(market["info"], raw_market)
        self.assertEqual(self.exchange.markets_by_id["ETHUSD:BTNL"], [market])
        self.assertEqual(self.exchange.symbols, ["ETH/USD:BTNL"])
        self.exchange.publicGetAssetPairs.assert_called_with(
            {"execution_venue": "bitnomial_exchange"}
        )

    def test_builds_slash_symbol_from_altname_when_wsname_is_missing(self):
        self.exchange.publicGetAssetPairs.return_value = {
            "result": {
                "ETHUSD:BTNL": {
                    "altname": "ETHUSD:BTNL",
                    "base": "XETH",
                    "quote": "ZUSD",
                    "lot_decimals": "8",
                    "tick_size": "0.01",
                }
            }
        }

        kraken._ensure_bitnomial_markets(self.exchange)

        self.assertIn("ETH/USD:BTNL", self.exchange.markets)

    def test_get_exchange_registers_markets_when_created(self):
        original_exchange = kraken._exchange
        self.addCleanup(setattr, kraken, "_exchange", original_exchange)
        kraken._exchange = None
        with (
            patch("kraken.ccxt.kraken", return_value=self.exchange),
            patch("kraken._ensure_bitnomial_markets") as ensure_markets,
        ):
            result = kraken.get_kraken_exchange()

        self.assertIs(result, self.exchange)
        ensure_markets.assert_called_once_with(self.exchange)


class QuoteCurrencyTests(unittest.TestCase):
    def test_extracts_base_quote_currency_from_margin_symbol(self):
        self.assertEqual(kraken._quote_currency("ETH/USD:BTNL"), "USD")

    def test_quote_balance_validation_allows_cover_with_used_margin(self):
        exchange = Mock()
        exchange.fetch_balance.return_value = {"free": {"USD": 50.0}}
        position = {
            "symbol": "ETH/USD:BTNL",
            "side": "short",
            "entry_price": 100.0,
            "size_base": 2.0,
            "used_margin_usd": 100.0,
        }
        trade = {
            "symbol": "ETH/USD:BTNL",
            "quote": "USD:BTNL",
            "asset": "ETH",
            "action": "cover_short",
            "amount_usd": 200.0,
        }

        with (
            patch("kraken.get_kraken_exchange", return_value=exchange),
            patch("kraken.fetch_margin_positions", return_value=[position]),
        ):
            warnings = kraken.validate_quote_balances([trade])

        self.assertEqual(warnings, [])

    def test_quote_balance_validation_reserves_short_proceeds_for_new_shorts(self):
        exchange = Mock()
        exchange.fetch_balance.return_value = {"free": {"USD": 500.0}}
        position = {
            "symbol": "ETH/USD:BTNL",
            "side": "short",
            "entry_price": 100.0,
            "size_base": 2.0,
            "used_margin_usd": 100.0,
        }
        trade = {
            "symbol": "BTC/USD:BTNL",
            "quote": "USD",
            "asset": "BTC",
            "action": "open_short",
            "amount_usd": 400.0,
            "capital_usd": 400.0,
        }

        with (
            patch("kraken.get_kraken_exchange", return_value=exchange),
            patch("kraken.fetch_margin_positions", return_value=[position]),
        ):
            warnings = kraken.validate_quote_balances([trade])

        self.assertEqual(len(warnings), 1)
        self.assertIn("requires $100.00 more USD", warnings[0])

    def test_quote_balance_validation_credits_spot_reductions_to_stable_balance(self):
        exchange = Mock()
        exchange.fetch_balance.return_value = {"free": {"USD": 500.0}}
        reduction = {
            "symbol": "ETH/USD:BTNL",
            "quote": "USD:BTNL",
            "action": "reduce_long",
            "amount_usd": 100.0,
            "margin": False,
        }
        cover = {
            "symbol": "ETH/USD:BTNL",
            "quote": "USD:BTNL",
            "asset": "ETH",
            "action": "cover_short",
            "amount_usd": 600.0,
        }

        with (
            patch("kraken.get_kraken_exchange", return_value=exchange),
            patch("kraken.fetch_margin_positions", return_value=[]),
        ):
            warnings = kraken.validate_quote_balances([reduction, cover])

        self.assertEqual(warnings, [])

    def test_quote_balance_validation_preserves_sdca_cash(self):
        exchange = Mock()
        exchange.fetch_balance.return_value = {"free": {"USD": 500.0}}
        trade = {
            "symbol": "BTC/USD",
            "asset": "BTC",
            "action": "open_long",
            "amount_usd": 300.0,
            "capital_usd": 300.0,
        }

        with (
            patch("kraken.get_kraken_exchange", return_value=exchange),
            patch("kraken.fetch_margin_positions", return_value=[]),
        ):
            warnings = kraken.validate_quote_balances(
                [trade], reserved_quote_balances={"USD": 250.0}
            )

        self.assertEqual(len(warnings), 1)
        self.assertIn("requires $50.00 more USD", warnings[0])


class MarginSymbolTests(unittest.TestCase):
    def setUp(self):
        self.exchange_patcher = patch("kraken.get_kraken_exchange")
        self.get_exchange = self.exchange_patcher.start()
        self.addCleanup(self.exchange_patcher.stop)
        self.exchange = self.get_exchange.return_value
        self.exchange.create_order.return_value = {"id": "test-order"}

    def test_market_margin_order_uses_bitnomial_symbol(self):
        result = kraken.create_market_order("ETH/USD", "sell", 1, margin=True)

        self.assertEqual(result, {"id": "test-order"})
        self.exchange.create_order.assert_called_once_with(
            "ETH/USD:BTNL", "market", "sell", 1, None, {"leverage": "2"}
        )

    def test_market_spot_order_keeps_original_symbol(self):
        kraken.create_market_order("ETH/USD", "buy", 1)

        self.exchange.create_order.assert_called_once_with(
            "ETH/USD", "market", "buy", 1, None, {}
        )

    def test_post_only_margin_order_uses_bitnomial_symbol(self):
        result = kraken.create_post_only_limit_order(
            "ETH/USD", "sell", 1, 2000, margin=True, reduce_only=True
        )

        self.assertEqual(result, {"id": "test-order"})
        self.exchange.create_order.assert_called_once_with(
            "ETH/USD:BTNL",
            "limit",
            "sell",
            1,
            2000,
            {"postOnly": True, "leverage": "2", "reduceOnly": True},
        )

    def test_post_only_spot_order_keeps_original_symbol(self):
        kraken.create_post_only_limit_order("ETH/USD", "buy", 1, 2000)

        self.exchange.create_order.assert_called_once_with(
            "ETH/USD", "limit", "buy", 1, 2000, {"postOnly": True}
        )

    def test_margin_symbol_does_not_duplicate_bitnomial_suffix(self):
        self.assertEqual(kraken._margin_symbol("ETH/USD:BTNL"), "ETH/USD:BTNL")


if __name__ == "__main__":
    unittest.main()
