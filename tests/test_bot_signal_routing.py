import os
import unittest
from unittest.mock import AsyncMock, Mock, patch

with patch.dict(
    os.environ,
    {"FASTAPI_URL": "http://localhost", "TELEGRAM_USER_ID": "1"},
):
    import bot


class SignalRoutingTests(unittest.IsolatedAsyncioTestCase):
    def test_sdca_override_accepts_percent_and_decimal_forms(self):
        for raw, expected in (
            ("8.05", 0.0805),
            ("8.05%", 0.0805),
            ("0.0805", 0.0805),
            ("-25", -0.25),
            ("-0.25", -0.25),
            ("0", 0.0),
        ):
            with self.subTest(raw=raw):
                self.assertAlmostEqual(bot.parse_sdca_override(raw), expected)

    def test_sdca_override_rejects_out_of_range_values(self):
        for raw in ("101", "-101%", "nan", "invalid"):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    bot.parse_sdca_override(raw)

    def test_detects_sdca_title_despite_markdown_and_emoji(self):
        text = "⚡ **sdca signal update** ⚡"
        self.assertEqual(bot.detect_signal_type(text), "sdca")

    def test_detects_rsps_title_case_insensitively(self):
        self.assertEqual(
            bot.detect_signal_type("⚡ **portfolio signal update** ⚡"),
            "rsps",
        )

    def test_mixed_titles_are_ambiguous_not_rsps(self):
        text = "SDCA Signal Update — Portfolio Signal Update"
        self.assertEqual(bot.detect_signal_type(text), "ambiguous")

    def test_unrelated_message_is_not_a_signal(self):
        self.assertIsNone(bot.detect_signal_type("Portfolio allocation update"))

    async def test_unparseable_sdca_signal_replies_without_calling_api(self):
        message = Mock()
        message.text = "⚡ SDCA Signal Update ⚡\nNothing to allocate"
        message.caption = None
        message.reply_text = AsyncMock()
        update = Mock()
        update.effective_user.id = 1
        update.message = message

        with (
            patch("bot.ALLOWED_USER_ID", 1),
            patch("bot.httpx.AsyncClient") as async_client,
        ):
            await bot.signal_update_handler(update, Mock())

        message.reply_text.assert_awaited_once()
        self.assertIn(
            "SDCA signal parse failed",
            message.reply_text.await_args.args[0],
        )
        async_client.assert_not_called()

    async def test_valid_sdca_signal_posts_signed_fraction_and_confirms_value(self):
        message = Mock()
        message.text = "SDCA Signal Update\n- SELL 25%"
        message.caption = None
        status_message = Mock()
        status_message.edit_text = AsyncMock()
        message.reply_text = AsyncMock(return_value=status_message)
        update = Mock()
        update.effective_user.id = 1
        update.message = message

        response = Mock()
        response.raise_for_status = Mock()
        client = Mock()
        client.post = AsyncMock(return_value=response)
        async_client = Mock()
        async_client.__aenter__ = AsyncMock(return_value=client)
        async_client.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("bot.ALLOWED_USER_ID", 1),
            patch("bot.REBALANCER_API_KEY", "test-key"),
            patch("bot.httpx.AsyncClient", return_value=async_client),
        ):
            await bot.signal_update_handler(update, Mock())

        client.post.assert_awaited_once_with(
            "http://localhost/updateSdcaChange",
            json={"change_pct": -0.25},
            headers={"X-API-Key": "test-key"},
        )
        status_message.edit_text.assert_awaited_once()
        self.assertIn("25.00% (sell)", status_message.edit_text.await_args.args[0])

    async def test_sdca_plan_blocks_when_combined_stables_are_insufficient(self):
        warning = "⚠️ CONVERSION NEEDED: buy BTC/USD requires $20.00 more USD."
        status_message = Mock()
        status_message.edit_text = AsyncMock()
        message = Mock()
        message.reply_text = AsyncMock(return_value=status_message)
        update = Mock()
        update.effective_user.id = 1
        update.message = message
        context = Mock()
        context.args = []

        plan = {
            "action": "buy",
            "amount_base": 0.0002,
            "amount_usd": 20.0,
            "cash_usd": 200.0,
            "kraken_btc": 0.0,
            "cold_btc": 0.0,
            "dry_run": False,
            "quote_warnings": [warning],
            "quote_insufficient": True,
        }
        response = Mock()
        response.raise_for_status = Mock()
        response.json.return_value = plan
        client = Mock()
        client.get = AsyncMock(return_value=response)
        async_client = Mock()
        async_client.__aenter__ = AsyncMock(return_value=client)
        async_client.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("bot.ALLOWED_USER_ID", 1),
            patch("bot.httpx.AsyncClient", return_value=async_client),
        ):
            await bot.sdca_command(update, context)

        text = status_message.edit_text.await_args.args[0]
        self.assertIn(warning, text)
        self.assertIn("Execution paused", text)
        self.assertNotIn("reply_markup", status_message.edit_text.await_args.kwargs)

    async def test_sdca_plan_blocks_confirmation_when_conversion_is_needed(self):
        warning = (
            "⚠️ CONVERSION NEEDED: Convert $22.05 USDG → USD to buy BTC/USD."
        )
        status_message = Mock()
        status_message.edit_text = AsyncMock()
        message = Mock()
        message.reply_text = AsyncMock(return_value=status_message)
        update = Mock()
        update.effective_user.id = 1
        update.message = message
        context = Mock()
        context.args = []

        plan = {
            "action": "buy",
            "amount_base": 0.0002,
            "amount_usd": 20.0,
            "cash_usd": 200.0,
            "kraken_btc": 0.0,
            "cold_btc": 0.0,
            "dry_run": False,
            "quote_warnings": [warning],
            "quote_insufficient": True,
        }
        response = Mock()
        response.raise_for_status = Mock()
        response.json.return_value = plan
        client = Mock()
        client.get = AsyncMock(return_value=response)
        async_client = Mock()
        async_client.__aenter__ = AsyncMock(return_value=client)
        async_client.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("bot.ALLOWED_USER_ID", 1),
            patch("bot.httpx.AsyncClient", return_value=async_client),
        ):
            await bot.sdca_command(update, context)

        text = status_message.edit_text.await_args.args[0]
        self.assertIn(warning, text)
        self.assertIn("Execution paused", text)
        self.assertNotIn("reply_markup", status_message.edit_text.await_args.kwargs)

    async def test_sdca_execute_failure_includes_api_response_detail(self):
        query = Mock()
        query.from_user.id = 1
        query.data = "sdca_confirm"
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()
        query.message.chat_id = 123
        query.message.message_id = 456
        update = Mock()
        update.callback_query = query

        response = bot.httpx.Response(
            400,
            json={"detail": "SDCA order amount is below Kraken's BTC precision"},
            request=bot.httpx.Request("POST", "http://localhost/sdca/execute"),
        )
        client = Mock()
        client.post = AsyncMock(return_value=response)
        async_client = Mock()
        async_client.__aenter__ = AsyncMock(return_value=client)
        async_client.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("bot.ALLOWED_USER_ID", 1),
            patch("bot.httpx.AsyncClient", return_value=async_client),
        ):
            await bot.button_callback(update, Mock())

        client.post.assert_awaited_once_with(
            "http://localhost/sdca/execute",
            json={"chat_id": 123, "message_id": 456},
            headers={"X-API-Key": bot.REBALANCER_API_KEY},
        )
        query.edit_message_text.assert_awaited()
        self.assertIn(
            "SDCA order amount is below Kraken's BTC precision",
            query.edit_message_text.await_args_list[-1].args[0],
        )

    async def test_rsps_execute_passes_message_identifiers_and_uses_reported_final(self):
        query = Mock()
        query.from_user.id = 1
        query.data = "confirm"
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()
        query.message.chat_id = 123
        query.message.message_id = 456
        update = Mock()
        update.callback_query = query

        response = Mock()
        response.raise_for_status = Mock()
        response.json.return_value = {
            "results": ["Filled BTC buy"],
            "progress_reported": True,
        }
        client = Mock()
        client.post = AsyncMock(return_value=response)
        async_client = Mock()
        async_client.__aenter__ = AsyncMock(return_value=client)
        async_client.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("bot.ALLOWED_USER_ID", 1),
            patch("bot.REBALANCER_API_KEY", "test-key"),
            patch("bot.httpx.AsyncClient", return_value=async_client),
        ):
            await bot.button_callback(update, Mock())

        client.post.assert_awaited_once_with(
            "http://localhost/rebalance/execute",
            json={"chat_id": 123, "message_id": 456},
            headers={"X-API-Key": "test-key"},
        )
        query.edit_message_text.assert_awaited_once_with(
            "🔄 Refreshing allocations…"
        )

    async def test_mixed_titles_reply_without_calling_api(self):
        message = Mock()
        message.text = "SDCA Signal Update; Portfolio Signal Update; BUY 10%"
        message.caption = None
        message.reply_text = AsyncMock()
        update = Mock()
        update.effective_user.id = 1
        update.message = message

        with (
            patch("bot.ALLOWED_USER_ID", 1),
            patch("bot.httpx.AsyncClient") as async_client,
        ):
            await bot.signal_update_handler(update, Mock())

        message.reply_text.assert_awaited_once()
        self.assertIn("both SDCA and Portfolio", message.reply_text.await_args.args[0])
        async_client.assert_not_called()


if __name__ == "__main__":
    unittest.main()
