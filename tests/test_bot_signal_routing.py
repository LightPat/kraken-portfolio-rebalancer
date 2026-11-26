import os
import unittest
from unittest.mock import AsyncMock, Mock, patch

with patch.dict(
    os.environ,
    {"FASTAPI_URL": "http://localhost", "TELEGRAM_USER_ID": "1"},
):
    import bot


class SignalRoutingTests(unittest.IsolatedAsyncioTestCase):
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
