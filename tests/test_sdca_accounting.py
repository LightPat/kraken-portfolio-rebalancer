import os
import unittest
from unittest.mock import patch

with patch.dict(
    os.environ,
    {"FASTAPI_URL": "http://localhost", "TELEGRAM_USER_ID": "1"},
):
    import rebalancer


class CapitalSplitTests(unittest.TestCase):
    def test_rsps_investable_value_uses_only_its_split_less_reserve(self):
        self.assertEqual(
            rebalancer.calculate_rsps_investable_value(10_000, 0.7, 500),
            6_500,
        )

    def test_rsps_investable_value_never_goes_below_zero(self):
        self.assertEqual(
            rebalancer.calculate_rsps_investable_value(1_000, 0.2, 500),
            0,
        )

    def test_rebalance_plan_applies_sheet_split_and_rsps_reserve(self):
        portfolio = (
            {},
            {},
            {},
            {},
            1_000.0,
            {"USD": 400.0, "USDC": 0.0, "USDG": 0.0},
        )
        with (
            patch("rebalancer.get_kraken_exchange"),
            patch(
                "rebalancer.update_current_allocations_in_sheet"
            ) as update_allocations,
            patch("rebalancer.fetch_portfolio", return_value=portfolio),
            patch("rebalancer.get_target_allocations", return_value={}),
            patch("rebalancer.fetch_margin_positions", return_value=[]),
            patch("rebalancer.get_desired_cash_reserve", return_value=100.0),
            patch("rebalancer.get_rsps_allocation_pct", return_value=0.7),
            patch(
                "rebalancer.get_sdca_sheet_values",
                return_value={
                    "cash_usd": 200.0,
                    "buy_pct": 0.1,
                    "kraken_btc": 0.0,
                    "cold_btc": 0.0,
                },
            ),
        ):
            plan = rebalancer.generate_rebalance_plan()

        self.assertEqual(plan["investable_value"], 600.0)
        update_allocations.assert_called_once_with()


class SdcaCashAccountingTests(unittest.TestCase):
    def test_sdca_plan_refreshes_allocations_before_reading_plan_inputs(self):
        with (
            patch("rebalancer.update_current_allocations_in_sheet") as refresh,
            patch("rebalancer.get_kraken_exchange") as exchange,
            patch(
                "rebalancer.get_sdca_sheet_values",
                return_value={
                    "cash_usd": 100.0,
                    "buy_pct": 0.1,
                    "kraken_btc": 0.0,
                    "cold_btc": 0.0,
                },
            ),
            patch("rebalancer.get_best_trading_symbol", return_value="BTC/USD"),
        ):
            exchange.return_value.fetch_ticker.return_value = {"last": 100.0}
            result = rebalancer.generate_sdca_plan("buy")

        refresh.assert_called_once_with()
        self.assertAlmostEqual(result["amount_usd"], 10.0)

    def test_sdca_dry_run_refreshes_allocations_after_plan(self):
        plan = {
            "action": "buy",
            "symbol": "BTC/USD",
            "amount_usd": 10.0,
            "amount_base": 0.1,
            "dry_run": True,
        }
        with (
            patch("rebalancer.generate_sdca_plan", return_value=plan),
            patch("rebalancer.update_current_allocations_in_sheet") as refresh,
        ):
            result = rebalancer.execute_sdca_trade("buy")

        refresh.assert_called_once_with()
        self.assertTrue(result["dry_run"])

    def test_buy_subtracts_actual_spend_from_sdca_cash(self):
        self.assertAlmostEqual(
            rebalancer.calculate_sdca_cash_balance(100, "buy", 8.656),
            91.344,
        )

    def test_sell_adds_actual_proceeds_to_sdca_cash(self):
        self.assertAlmostEqual(
            rebalancer.calculate_sdca_cash_balance(91.344, "sell", 20),
            111.344,
        )

    def test_buy_updates_cash_from_filled_order_cost_and_fee(self):
        plan = {
            "action": "buy",
            "symbol": "BTC/USD",
            "amount_usd": 10.0,
            "amount_base": 0.1,
            "dry_run": False,
        }
        order = {
            "id": "buy-1",
            "status": "closed",
            "amount": 0.1,
            "filled": 0.1,
            "cost": 10.0,
            "average": 100.0,
            "fee": {"cost": 0.1, "currency": "USD"},
        }

        with (
            patch("rebalancer.generate_sdca_plan", return_value=plan),
            patch("rebalancer.get_free_balance", return_value=100.0),
            patch("rebalancer.get_kraken_exchange") as get_exchange,
            patch("rebalancer.get_safe_post_only_price", return_value=100.0),
            patch("rebalancer.create_post_only_limit_order", return_value=order),
            patch("rebalancer.fetch_order", return_value=order),
            patch(
                "rebalancer.get_sdca_sheet_values",
                return_value={
                    "cash_usd": 100.0,
                    "buy_pct": 0.1,
                    "kraken_btc": 0.05,
                    "cold_btc": 0.2,
                },
            ),
            patch("rebalancer.update_sdca_cash_in_sheet") as update_cash,
            patch("rebalancer.update_current_allocations_in_sheet") as update_allocations,
        ):
            get_exchange.return_value.fetch_ticker.return_value = {"last": 100.0}
            result = rebalancer.execute_sdca_trade("buy")

        update_cash.assert_called_once_with(89.9)
        update_allocations.assert_called_once_with(
            sdca_btc_adjustment=0.1, force_cold_balance_refresh=True
        )
        self.assertAlmostEqual(result["actual_quote_amount_usd"], 10.1)

    def test_sell_returns_actual_proceeds_and_reduces_tracked_btc(self):
        plan = {
            "action": "sell",
            "symbol": "BTC/USD",
            "amount_usd": 5.0,
            "amount_base": 0.05,
            "dry_run": False,
        }
        order = {
            "id": "sell-1",
            "status": "closed",
            "amount": 0.05,
            "filled": 0.04,
            "cost": 4.1,
            "average": 102.5,
            "fee": {"cost": 0.1, "currency": "USD"},
        }

        with (
            patch("rebalancer.generate_sdca_plan", return_value=plan),
            patch("rebalancer.get_free_balance", return_value=0.1),
            patch("rebalancer.get_kraken_exchange") as get_exchange,
            patch("rebalancer.get_safe_post_only_price", return_value=100.0),
            patch("rebalancer.create_post_only_limit_order", return_value=order),
            patch("rebalancer.fetch_order", return_value=order),
            patch(
                "rebalancer.get_sdca_sheet_values",
                return_value={
                    "cash_usd": 100.0,
                    "buy_pct": 0.1,
                    "kraken_btc": 0.1,
                    "cold_btc": 0.2,
                },
            ),
            patch("rebalancer.update_sdca_cash_in_sheet") as update_cash,
            patch("rebalancer.update_current_allocations_in_sheet") as update_allocations,
        ):
            get_exchange.return_value.fetch_ticker.return_value = {"last": 100.0}
            result = rebalancer.execute_sdca_trade("sell", 50)

        update_cash.assert_called_once_with(104.0)
        update_allocations.assert_called_once_with(
            sdca_btc_adjustment=-0.04, force_cold_balance_refresh=True
        )
        self.assertAlmostEqual(result["actual_quote_amount_usd"], 4.0)


if __name__ == "__main__":
    unittest.main()
