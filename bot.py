import os
import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# CONFIG FROM ENV
FASTAPI_URL = os.getenv("FASTAPI_URL").rstrip("/")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_KRAKEN_PORTFOLIO_REBALANCER_BOT_HTTP_TOKEN")
ALLOWED_USER_ID = int(os.getenv("TELEGRAM_USER_ID"))
REBALANCER_API_KEY = os.getenv("REBALANCER_API_KEY")

# Webhook config
USE_WEBHOOK = os.getenv("USE_WEBHOOK", "true").lower() == "true"
TELEGRAM_WEBHOOK_URL = os.getenv("TELEGRAM_WEBHOOK_URL")
TELEGRAM_WEBHOOK_PORT = int(os.getenv("TELEGRAM_WEBHOOK_PORT", "8443"))

# Constants
HTTP_TIMEOUT_SECONDS = 600.0
CONNECT_TIMEOUT_SECONDS = 10.0
CRYPTO_DECIMALS = 6
USD_DECIMALS = 2
PRICE_DECIMALS = 4


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 Kraken Rebalancer ready!\n\nCommands:\n/rebalance\n/cancelRebalance\n/updateCurrentAllocations"
    )


async def update_current_allocations_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    if update.effective_user.id != ALLOWED_USER_ID:
        await update.message.reply_text("❌ Unauthorized")
        return

    status_message = await update.message.reply_text(
        "🔄 Updating current allocations..."
    )

    try:
        timeout = httpx.Timeout(HTTP_TIMEOUT_SECONDS, connect=CONNECT_TIMEOUT_SECONDS)
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                f"{FASTAPI_URL}/updateCurrentAllocations",
                headers={"X-API-Key": REBALANCER_API_KEY},
            )
            resp.raise_for_status()
            result = resp.json()
    except Exception as e:
        await status_message.edit_text(f"❌ Execute failed: {type(e).__name__}: {e}")
        return

    result_text = "\n".join(result.get("results", ["No details returned"]))
    await status_message.edit_text(f"🚀 Google sheet updated!\n\n{result_text}")


async def cancel_rebalance_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER_ID:
        await update.message.reply_text("❌ Unauthorized")
        return

    status_message = await update.message.reply_text(
        "🛑 Sending cancel request to rebalance..."
    )

    try:
        timeout = httpx.Timeout(HTTP_TIMEOUT_SECONDS, connect=CONNECT_TIMEOUT_SECONDS)
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                f"{FASTAPI_URL}/rebalance/cancel",
                headers={"X-API-Key": REBALANCER_API_KEY},
            )
            resp.raise_for_status()
            result = resp.json()
    except Exception as e:
        await status_message.edit_text(
            f"❌ Cancel request failed: {type(e).__name__}: {e}"
        )
        return

    await status_message.edit_text(f"🛑 Cancel requested. {result.get('message', '')}")


async def rebalance_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER_ID:
        await update.message.reply_text("❌ Unauthorized")
        return

    status_message = await update.message.reply_text("🔄 Starting rebalance process...")

    try:
        timeout = httpx.Timeout(HTTP_TIMEOUT_SECONDS, connect=CONNECT_TIMEOUT_SECONDS)
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(
                f"{FASTAPI_URL}/rebalance/plan",
                headers={"X-API-Key": REBALANCER_API_KEY},
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        await status_message.edit_text(
            f"❌ Failed to get plan: {type(e).__name__}: {e}"
        )
        return

    if not data["plan"]:
        await status_message.edit_text("✅ Portfolio is already balanced!")
        return

    lines = [
        "📊 Rebalance Plan",
        f"• Total Portfolio Value: `${round(data.get('total_value_usd', 0), USD_DECIMALS)}`",
        f"• Desired Cash Reserve: `${round(data.get('desired_reserve', 0), USD_DECIMALS)}`",
        f"• Investable Value: `${round(data.get('investable_value', 0), USD_DECIMALS)}`",
        f"• Current Stables (USD+USDC+USDG): `${round(data.get('current_stables_total', 0), USD_DECIMALS)}`",
        "\n",
    ]

    plan_text = "\n".join(lines)

    for t in data["plan"]:
        plan_text += f"{t['action'].upper()} {round(t['amount_base'], CRYPTO_DECIMALS)} {t['asset']} (~${round(t['amount_usd'], USD_DECIMALS)})\n"

    keyboard = [
        [
            InlineKeyboardButton("✅ Execute", callback_data="confirm"),
            InlineKeyboardButton("❌ Cancel", callback_data="cancel"),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await status_message.edit_text(
        f"{plan_text}\nDRY_RUN = {data['dry_run']}\n\nExecute these trades?",
        reply_markup=reply_markup,
    )


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    if user_id != ALLOWED_USER_ID:
        await query.edit_message_text("❌ Unauthorized")
        return

    if query.data == "cancel":
        await query.edit_message_text("❌ Rebalance cancelled.")
        return

    if query.data == "confirm":
        await query.edit_message_text("🔄 Rebalance in progress...")
        try:
            timeout = httpx.Timeout(
                HTTP_TIMEOUT_SECONDS, connect=CONNECT_TIMEOUT_SECONDS
            )
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(
                    f"{FASTAPI_URL}/rebalance/execute",
                    headers={"X-API-Key": REBALANCER_API_KEY},
                )
                resp.raise_for_status()
                result = resp.json()
        except Exception as e:
            await query.edit_message_text(f"❌ Execute failed: {type(e).__name__}: {e}")
            return

        result_text = "\n".join(result.get("results", ["No details returned"]))
        await query.edit_message_text(f"🚀 Rebalance executed!\n\n{result_text}")


async def signal_update_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Automatically parse any message containing a Portfolio Signal Update."""
    if update.effective_user.id != ALLOWED_USER_ID:
        return

    text = (update.message.text or update.message.caption or "").strip()
    if "Portfolio Signal Update" not in text or "RSPS Signal:" not in text:
        return  # not a signal message

    status_message = await update.message.reply_text(
        "🔄 Parsing signal & updating targets..."
    )

    try:
        timeout = httpx.Timeout(HTTP_TIMEOUT_SECONDS, connect=CONNECT_TIMEOUT_SECONDS)
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                f"{FASTAPI_URL}/updateTargetsFromSignal",
                json={"signal_text": text},
                headers={"X-API-Key": REBALANCER_API_KEY},
            )
            resp.raise_for_status()
            result = resp.json()
    except Exception as e:
        await status_message.edit_text(
            f"❌ Signal update failed: {type(e).__name__}: {e}"
        )
        return

    await status_message.edit_text(
        f"🚀 Signal processed!\n\n{result.get('message', 'Done')}"
    )


def main():
    app = Application.builder().token(TELEGRAM_TOKEN).concurrent_updates(True).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(
        CommandHandler("updateCurrentAllocations", update_current_allocations_command)
    )
    app.add_handler(CommandHandler("rebalance", rebalance_command))
    app.add_handler(CommandHandler("cancelRebalance", cancel_rebalance_command))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, signal_update_handler)
    )

    if USE_WEBHOOK and TELEGRAM_WEBHOOK_URL:
        print(f"🤖 Telegram bot starting with **webhooks** → {TELEGRAM_WEBHOOK_URL}")
        app.run_webhook(
            listen="0.0.0.0",
            port=TELEGRAM_WEBHOOK_PORT,
            url_path="telegram-webhook",  # must match the end of TELEGRAM_WEBHOOK_URL
            webhook_url=TELEGRAM_WEBHOOK_URL,  # full public HTTPS URL
        )
    else:
        print("🤖 Telegram bot starting (polling)...")
        app.run_polling()


if __name__ == "__main__":
    main()
