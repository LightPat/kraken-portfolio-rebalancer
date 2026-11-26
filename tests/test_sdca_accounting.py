import os
import unittest
from unittest.mock import Mock, patch

with patch.dict(
    os.environ,
    {"FASTAPI_URL": "http://localhost", "TELEGRAM_USER_ID": "1"},
):
    import rebalancer


class CapitalSplitTests(unittest.TestCase):
    def test_rsps_investable_value_matches_sheet_formula_without_split(self):
        self.assertEqual(
            rebalancer.calculate_rsps_investable_value(10_000, 500, 300),
            9_200,
        )

    def test_rsps_investable_value_never_goes_below_zero(self):
        self.assertEqual(
            rebalancer.calculate_rsps_investable_value(1_000, 800, 300),
            0,
        )

    def test_rebalance_plan_matches_formula_and_keeps_split_for_display(self):
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
            patch("rebalancer.get_sdca_cash_on_kraken", return_value=50.0),
            patch(
                "rebalancer.get_sdca_sheet_values",
                return_value={
                    "cash_usd": 200.0,
                    "change_pct": 0.1,
                    "kraken_btc": 0.0,
                    "cold_btc": 0.0,
                },
            ),
        ):
            plan = rebalancer.generate_rebalance_plan()

        self.assertEqual(plan["investable_value"], 850.0)
        self.assertEqual(plan["rsps_allocation_pct"], 0.7)
        self.assertEqual(plan["current_stables_total"], 350.0)
        update_allocations.assert_called_once_with()


class SdcaCashAccountingTests(unittest.TestCase):
    def test_zero_change_returns_empty_plan_without_fetching_market(self):
        with (
            patch("rebalancer.update_current_allocations_in_sheet"),
            patch("rebalancer.get_sdca_sheet_values", return_value={
                "cash_usd": 20.0,
                "change_pct": 0.0,
                "kraken_btc": 0.1,
                "cold_btc": 0.3,
            }),
            patch("rebalancer.get_kraken_exchange") as exchange,
        ):
            plan = rebalancer.generate_sdca_plan()

        self.assertIsNone(plan["action"])
        self.assertEqual(plan["amount_base"], 0.0)
        exchange.assert_not_called()

    def test_signed_negative_change_sells_percent_of_total_sdca_btc(self):
        with (
            patch("rebalancer.update_current_allocations_in_sheet"),
            patch("rebalancer.get_sdca_sheet_values", return_value={
                "cash_usd": 20.0,
                "change_pct": -0.5,
                "kraken_btc": 0.3,
                "cold_btc": 0.1,
            }),
            patch("rebalancer.get_kraken_exchange") as exchange,
            patch("rebalancer.get_best_trading_symbol", return_value="BTC/USD"),
            patch("rebalancer.get_free_balance", return_value=0.15),
        ):
            exchange.return_value.fetch_ticker.return_value = {"last": 100.0}
            plan = rebalancer.generate_sdca_plan()

        self.assertEqual(plan["action"], "sell")
        self.assertAlmostEqual(plan["target_btc"], 0.2)
        self.assertAlmostEqual(plan["amount_base"], 0.15)
        self.assertAlmostEqual(plan["unavailable_btc"], 0.05)

    def test_sell_does_not_use_free_btc_attributed_to_rsps(self):
        with (
            patch("rebalancer.update_current_allocations_in_sheet"),
            patch("rebalancer.get_sdca_sheet_values", return_value={
                "cash_usd": 20.0,
                "change_pct": -0.5,
                "kraken_btc": 0.1,
                "cold_btc": 0.3,
            }),
            patch("rebalancer.get_kraken_exchange") as exchange,
            patch("rebalancer.get_best_trading_symbol", return_value="BTC/USD"),
            patch("rebalancer.get_free_balance", return_value=0.5),
        ):
            exchange.return_value.fetch_ticker.return_value = {"last": 100.0}
            plan = rebalancer.generate_sdca_plan()

        self.assertAlmostEqual(plan["target_btc"], 0.2)
        self.assertAlmostEqual(plan["amount_base"], 0.1)
        self.assertAlmostEqual(plan["unavailable_btc"], 0.1)

    def test_signed_positive_change_buys_percent_of_sdca_cash(self):
        with (
            patch("rebalancer.update_current_allocations_in_sheet"),
            patch("rebalancer.get_sdca_sheet_values", return_value={
                "cash_usd": 200.0,
                "change_pct": 0.25,
                "kraken_btc": 0.1,
                "cold_btc": 0.3,
            }),
            patch("rebalancer.get_kraken_exchange") as exchange,
            patch("rebalancer.get_best_trading_symbol", return_value="BTC/USD"),
            patch(
                "rebalancer.validate_quote_balances",
                return_value=Mock(warnings=[], insufficient=False),
            ),
        ):
            exchange.return_value.fetch_ticker.return_value = {"last": 100.0}
            plan = rebalancer.generate_sdca_plan()

        self.assertEqual(plan["action"], "buy")
        self.assertAlmostEqual(plan["amount_usd"], 50.0)
        self.assertEqual(plan["quote_warnings"], [])

    def test_buy_plan_reports_quote_shortfall_before_confirmation(self):
        warning = "⚠️ CONVERSION NEEDED: buy BTC/USD requires $20.00 more USD."
        with (
            patch("rebalancer.update_current_allocations_in_sheet"),
            patch(
                "rebalancer.get_sdca_sheet_values",
                return_value={
                    "cash_usd": 200.0,
                    "change_pct": 0.1,
                    "kraken_btc": 0.0,
                    "cold_btc": 0.0,
                },
            ),
            patch("rebalancer.get_kraken_exchange") as exchange,
            patch("rebalancer.get_best_trading_symbol", return_value="BTC/USD"),
            patch(
                "rebalancer.validate_quote_balances",
                return_value=Mock(warnings=[warning], insufficient=True),
            ) as validate_balances,
        ):
            exchange.return_value.fetch_ticker.return_value = {"last": 100.0}
            plan = rebalancer.generate_sdca_plan()

        self.assertEqual(plan["quote_warnings"], [warning])
        validate_balances.assert_called_once_with(
            [
                {
                    "asset": "BTC",
                    "action": "buy",
                    "amount_usd": 20.0,
                    "symbol": "BTC/USD",
                    "quote": "USD",
                    "margin": False,
                }
            ]
        )

    def test_sdca_plan_refreshes_allocations_before_reading_plan_inputs(self):
        with (
            patch("rebalancer.update_current_allocations_in_sheet") as refresh,
            patch("rebalancer.get_kraken_exchange") as exchange,
            patch(
                "rebalancer.get_sdca_sheet_values",
                return_value={
                    "cash_usd": 100.0,
                    "change_pct": 0.1,
                    "kraken_btc": 0.0,
                    "cold_btc": 0.0,
                },
            ),
            patch("rebalancer.get_best_trading_symbol", return_value="BTC/USD"),
            patch(
                "rebalancer.validate_quote_balances",
                return_value=Mock(warnings=[], insufficient=False),
            ),
        ):
            exchange.return_value.fetch_ticker.return_value = {"last": 100.0}
            result = rebalancer.generate_sdca_plan()

        refresh.assert_called_once_with()
        self.assertAlmostEqual(result["amount_usd"], 10.0)

    def test_execute_pauses_when_plan_reports_quote_shortfall(self):
        warning = "⚠️ CONVERSION NEEDED: buy BTC/USD requires $20.00 more USD."
        plan = {
            "action": "buy",
            "symbol": "BTC/USD",
            "amount_usd": 20.0,
            "amount_base": 0.2,
            "quote_warnings": [warning],
            "quote_insufficient": True,
            "dry_run": False,
        }
        with (
            patch("rebalancer.generate_sdca_plan", return_value=plan),
            patch("rebalancer.get_free_balance") as get_free_balance,
            patch("rebalancer.create_post_only_limit_order") as create_order,
        ):
            result = rebalancer.execute_sdca_trade()

        self.assertEqual(result["status"], "blocked")
        self.assertIn(warning, result["results"])
        get_free_balance.assert_not_called()
        create_order.assert_not_called()

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
            patch("rebalancer.update_sdca_cash_after_fill") as update_cash,
            patch("rebalancer.update_sdca_change_in_sheet") as clear_change,
        ):
            result = rebalancer.execute_sdca_trade()

        refresh.assert_called_once_with()
        update_cash.assert_not_called()
        clear_change.assert_not_called()
        self.assertTrue(result["dry_run"])

    def test_noop_trade_does_not_clear_change_pct(self):
        plan = {
            "action": None,
            "amount_base": 0.0,
            "dry_run": False,
        }
        with (
            patch("rebalancer.generate_sdca_plan", return_value=plan),
            patch("rebalancer.update_sdca_cash_after_fill") as update_cash,
            patch("rebalancer.update_sdca_change_in_sheet") as clear_change,
        ):
            result = rebalancer.execute_sdca_trade()

        update_cash.assert_not_called()
        clear_change.assert_not_called()
        self.assertIn("nothing to do", result["results"][0])

    def test_buy_records_fill_and_clears_q4_without_reading_q3(self):
        plan = {
            "action": "buy",
            "symbol": "BTC/USD",
            "amount_usd": 10.0,
            "amount_base": 0.1,
            "target_btc": 0.0,
            "unavailable_btc": 0.0,
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
        sheet_update_order = []

        with (
            patch("rebalancer.generate_sdca_plan", return_value=plan),
            patch("rebalancer.get_free_balance", return_value=100.0),
            patch("rebalancer.get_kraken_exchange") as get_exchange,
            patch("rebalancer.get_safe_post_only_price", return_value=100.0),
            patch("rebalancer.create_post_only_limit_order", return_value=order),
            patch("rebalancer.fetch_order", return_value=order),
            patch(
                "rebalancer.update_current_allocations_in_sheet",
                side_effect=lambda **_kwargs: sheet_update_order.append(
                    "allocations"
                ),
            ) as update_allocations,
            patch(
                "rebalancer.update_sdca_cash_after_fill",
                side_effect=lambda *_args: (
                    sheet_update_order.append("cash")
                    or {"usd_left": 189.9, "cash_on_kraken": 89.9}
                ),
            ) as update_cash,
            patch("rebalancer.update_sdca_change_in_sheet") as clear_change,
            patch("rebalancer.get_sdca_sheet_values") as read_sdca,
            patch("rebalancer.report_progress") as report,
        ):
            get_exchange.return_value.fetch_ticker.return_value = {"last": 100.0}
            result = rebalancer.execute_sdca_trade(progress=Mock())

        read_sdca.assert_not_called()
        update_cash.assert_called_once_with("buy", 10.1)
        update_allocations.assert_called_once_with(
            sdca_btc_adjustment=0.1, force_cold_balance_refresh=True
        )
        self.assertEqual(sheet_update_order, ["cash", "allocations"])
        clear_change.assert_called_once_with(0.0)
        self.assertAlmostEqual(result["actual_quote_amount_usd"], 10.1)
        self.assertTrue(
            any("Change % Today cleared." in line for line in result["results"])
        )
        self.assertTrue(
            any(
                "SDCA USD Left: $189.90; SDCA Cash on Kraken: $89.90" in line
                for line in result["results"]
            )
        )
        progress_texts = [call.args[1] for call in report.call_args_list]
        self.assertTrue(any("placing post-only order" in text for text in progress_texts))
        self.assertTrue(
            any("post-only order placed; waiting for fill" in text for text in progress_texts)
        )
        self.assertTrue(any("filled." in text for text in progress_texts))
        self.assertIn("Updating sheet…", progress_texts)
        self.assertIn("Sheet updated; Change % Today cleared.", progress_texts)

    def test_progress_helper_swallows_telegram_edit_failure(self):
        with (
            patch.dict(
                os.environ,
                {rebalancer.TELEGRAM_TOKEN_ENV: "test-token"},
            ),
            patch(
                "rebalancer.httpx.post",
                side_effect=RuntimeError("Telegram API unavailable"),
            ),
        ):
            reporter = rebalancer.TelegramProgressReporter(123, 456)
            reporter.last_edit_at -= 3
            with self.assertLogs("rebalancer", level="WARNING"):
                self.assertFalse(
                    rebalancer.report_progress(reporter, "Progress update")
                )

    def test_progress_updates_are_rate_limited(self):
        response = Mock()
        response.json.return_value = {"ok": True}
        with (
            patch.dict(
                os.environ,
                {rebalancer.TELEGRAM_TOKEN_ENV: "test-token"},
            ),
            patch("rebalancer.httpx.post", return_value=response) as post,
        ):
            reporter = rebalancer.TelegramProgressReporter(123, 456)
            reporter.last_edit_at -= 3
            self.assertTrue(rebalancer.report_progress(reporter, "First update"))
            self.assertFalse(rebalancer.report_progress(reporter, "Second update"))

        post.assert_called_once()

    def test_failed_trade_does_not_clear_change_pct(self):
        plan = {
            "action": "buy",
            "symbol": "BTC/USD",
            "amount_usd": 10.0,
            "amount_base": 0.1,
            "dry_run": False,
        }
        with (
            patch("rebalancer.generate_sdca_plan", return_value=plan),
            patch("rebalancer.get_free_balance", return_value=0.0),
            patch("rebalancer.update_sdca_cash_after_fill") as update_cash,
            patch("rebalancer.update_sdca_change_in_sheet") as clear_change,
        ):
            with self.assertRaisesRegex(ValueError, "only \\$0.00 USD"):
                rebalancer.execute_sdca_trade()

        update_cash.assert_not_called()
        clear_change.assert_not_called()

    def test_sell_returns_actual_proceeds_and_reduces_tracked_btc(self):
        plan = {
            "action": "sell",
            "symbol": "BTC/USD",
            "amount_usd": 5.0,
            "amount_base": 0.05,
            "target_btc": 0.05,
            "unavailable_btc": 0.0,
            "kraken_btc": 0.1,
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
            patch("rebalancer.update_current_allocations_in_sheet") as update_allocations,
            patch(
                "rebalancer.update_sdca_cash_after_fill",
                return_value={"usd_left": 104.0, "cash_on_kraken": 44.0},
            ) as update_cash,
            patch("rebalancer.update_sdca_change_in_sheet") as clear_change,
            patch("rebalancer.get_sdca_sheet_values") as read_sdca,
        ):
            get_exchange.return_value.fetch_ticker.return_value = {"last": 100.0}
            result = rebalancer.execute_sdca_trade()

        read_sdca.assert_not_called()
        update_cash.assert_called_once()
        self.assertEqual(update_cash.call_args.args[0], "sell")
        self.assertAlmostEqual(update_cash.call_args.args[1], 4.0)
        update_allocations.assert_called_once_with(
            sdca_btc_adjustment=-0.04, force_cold_balance_refresh=True
        )
        clear_change.assert_called_once_with(0.0)
        self.assertAlmostEqual(result["actual_quote_amount_usd"], 4.0)
        self.assertTrue(
            any("$4.00 proceeds" in line for line in result["results"])
        )
        self.assertTrue(
            any("0.01000000 BTC" in line for line in result["results"])
        )


if __name__ == "__main__":
    unittest.main()
