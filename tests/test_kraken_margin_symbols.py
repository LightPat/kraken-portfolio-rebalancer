import unittest
from unittest.mock import patch

import kraken


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
