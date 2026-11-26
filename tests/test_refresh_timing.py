import asyncio
import os
import unittest
from contextlib import nullcontext
from unittest.mock import Mock, patch

with patch.dict(
    os.environ,
    {"FASTAPI_URL": "http://localhost", "TELEGRAM_USER_ID": "1"},
):
    import app


class AllocationRefreshTimingTests(unittest.TestCase):
    def test_rsps_execute_refreshes_before_plan_and_after_completion(self):
        plan = {
            "plan": [],
            "dry_run": False,
            "total_value_usd": 100.0,
        }
        refresh = Mock()

        def generate_plan(progress=None):
            refresh()
            return plan

        with (
            patch("app.rebalance_lock.acquire", return_value=nullcontext()),
            patch("app.reset_cancel_rebalance"),
            patch("app.generate_rebalance_plan", side_effect=generate_plan),
            patch("app.update_current_allocations_in_sheet", wraps=refresh),
        ):
            result = asyncio.run(app.execute_rebalance())

        self.assertEqual(result["status"], "already_balanced")
        self.assertEqual(refresh.call_count, 2)

    def test_rsps_execute_passes_progress_reporter_through_to_rebalancer(self):
        plan = {
            "plan": [{"action": "open_long", "asset": "BTC"}],
            "dry_run": False,
            "total_value_usd": 1_000.0,
            "desired_reserve": 100.0,
            "investable_value": 900.0,
            "current_stables_total": 200.0,
        }
        reporter = Mock()
        reporter.enabled = True
        with (
            patch("app.rebalance_lock.acquire", return_value=nullcontext()),
            patch("app.reset_cancel_rebalance"),
            patch("app.TelegramProgressReporter", return_value=reporter) as make_reporter,
            patch("app.generate_rebalance_plan", return_value=plan) as generate_plan,
            patch("app.execute_trades", return_value=["Filled BTC buy"]) as execute,
            patch("app.update_current_allocations_in_sheet"),
            patch("app.report_progress", side_effect=[False, False, True]) as report,
        ):
            result = asyncio.run(
                app.execute_rebalance(
                    app.ExecuteProgressRequest(chat_id=123, message_id=456)
                )
            )

        make_reporter.assert_called_once_with(123, 456)
        generate_plan.assert_called_once_with(progress=reporter)
        execute.assert_called_once_with(plan["plan"], progress=reporter)
        self.assertTrue(result["progress_reported"])
        self.assertEqual(
            report.call_args_list[-1].args[1],
            "🚀 Rebalance executed!\n\nFilled BTC buy",
        )


if __name__ == "__main__":
    unittest.main()
