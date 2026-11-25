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
            orders = rebalancer._submit_trade_orders(exchange, [trade], False, [])

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

    def test_terminal_order_needing_fallback_is_returned(self):
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
        final_statuses = [
            None,
            {"status": "canceled", "amount": 2, "filled": 2},
            {"status": "expired", "amount": 2, "filled": 2},
            {"status": "closed", "amount": 2, "filled": 1},
        ]

        for final_status in final_statuses:
            with (
                self.subTest(final_status=final_status),
                patch("rebalancer.is_rebalance_cancel_requested", return_value=False),
                patch("rebalancer.get_open_orders", return_value=[]),
                patch(
                    "rebalancer.fetch_order", return_value=final_status
                ) as fetch_order,
            ):
                fallback_orders = rebalancer._wait_for_order_completion(
                    [order], [], timeout_seconds=10, poll_interval_seconds=0
                )

            self.assertEqual(fallback_orders, [order])
            fetch_order.assert_called_once_with("order-1", "ETH/USD:BTNL")

    def test_filled_order_missing_from_open_orders_needs_no_fallback(self):
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
            patch("rebalancer.is_rebalance_cancel_requested", return_value=False),
            patch("rebalancer.get_open_orders", return_value=[]),
            patch(
                "rebalancer.fetch_order",
                return_value={"status": "closed", "amount": 2, "filled": 2},
            ),
        ):
            fallback_orders = rebalancer._wait_for_order_completion(
                [order], [], timeout_seconds=10, poll_interval_seconds=0
            )

        self.assertEqual(fallback_orders, [])

    def test_cleanup_includes_spot_and_margin_symbols(self):
        plan = [{"symbol": "ETH/USD", "action": "reduce_short"}]
        with (
            patch("rebalancer.os.getenv", return_value="false"),
            patch("rebalancer.get_kraken_exchange"),
            patch("rebalancer.cancel_open_orders", return_value=[]) as cancel,
            patch("rebalancer.validate_quote_balances", return_value=[]),
            patch(
                "rebalancer.get_sdca_sheet_values",
                return_value={
                    "cash_usd": 0.0,
                    "buy_pct": 0.0,
                    "kraken_btc": 0.0,
                    "cold_btc": 0.0,
                },
            ),
            patch("rebalancer._submit_trade_orders", return_value=[]),
            patch("rebalancer.generate_rebalance_plan", return_value={"plan": []}),
            patch("rebalancer.is_rebalance_cancel_requested", return_value=False),
        ):
            rebalancer.execute_trades(plan)

        cancel.assert_called_once_with(["ETH/USD", "ETH/USD:BTNL"])


if __name__ == "__main__":
    unittest.main()
