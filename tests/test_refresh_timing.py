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

        def generate_plan():
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


if __name__ == "__main__":
    unittest.main()
