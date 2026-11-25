import os
import unittest
from unittest.mock import Mock, patch

with patch.dict(
    os.environ,
    {"FASTAPI_URL": "http://localhost", "TELEGRAM_USER_ID": "1"},
):
    import rebalancer


class RebalancerMarginOrderTests(unittest.TestCase):
    def test_submitted_short_order_stores_margin_symbol(self):
        exchange = Mock()
        exchange.fetch_ticker.return_value = {"last": 100}
        trade = {
            "asset": "ETH",
            "action": "cover_short",
            "amount_base": 2,
            "capital_usd": 100,
            "symbol": "ETH/USD",
            "quote": "USD",
        }

        with (
            patch("rebalancer.get_safe_post_only_price", return_value=99),
            patch(
                "rebalancer.create_post_only_limit_order",
                return_value={"id": "order-1"},
            ) as create_order,
        ):
            orders = rebalancer._submit_trade_orders(
                exchange, [trade], False, []
            )

        self.assertEqual(orders[0]["symbol"], "ETH/USD:BTNL")
        create_order.assert_called_once_with(
            "ETH/USD", "buy", 2, 99, margin=True, reduce_only=True
        )

    def test_failed_refresh_falls_back_to_original_amount_on_margin_symbol(self):
        order = {
            "order_id": "order-1",
            "symbol": "ETH/USD:BTNL",
            "side": "buy",
            "amount_base": 2,
            "asset": "ETH",
            "action": "cover_short",
            "margin": True,
            "reduce_only": True,
        }

        with (
            patch("rebalancer.fetch_order", return_value=None) as fetch_order,
            patch("rebalancer.cancel_order") as cancel_order,
            patch(
                "rebalancer.create_market_order", return_value={"id": "market-1"}
            ) as create_market,
        ):
            rebalancer._cancel_and_market_fallback([order], [])

        fetch_order.assert_called_once_with("order-1", "ETH/USD:BTNL")
        cancel_order.assert_called_once_with("order-1")
        create_market.assert_called_once_with(
            "ETH/USD:BTNL", "buy", 2, margin=True, reduce_only=True
        )

    def test_cleanup_includes_spot_and_margin_symbols(self):
        plan = [{"symbol": "ETH/USD", "action": "reduce_short"}]
        with (
            patch("rebalancer.os.getenv", return_value="false"),
            patch("rebalancer.get_kraken_exchange"),
            patch("rebalancer.cancel_open_orders", return_value=[]) as cancel,
            patch("rebalancer.validate_quote_balances", return_value=[]),
            patch("rebalancer._submit_trade_orders", return_value=[]),
            patch("rebalancer.generate_rebalance_plan", return_value={"plan": []}),
            patch("rebalancer.is_rebalance_cancel_requested", return_value=False),
        ):
            rebalancer.execute_trades(plan)

        cancel.assert_called_once_with(["ETH/USD", "ETH/USD:BTNL"])


if __name__ == "__main__":
    unittest.main()
