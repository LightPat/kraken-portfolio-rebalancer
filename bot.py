import os
import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

FASTAPI_URL = os.getenv("FASTAPI_URL").rstrip("/")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_KRAKEN_PORTFOLIO_REBALANCER_BOT_HTTP_TOKEN")
ALLOWED_USER_ID = int(os.getenv("TELEGRAM_USER_ID"))


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 Kraken Rebalancer ready!\n\nUse /rebalance to start."
    )


async def rebalance_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER_ID:
        await update.message.reply_text("❌ Unauthorized")
        return

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{FASTAPI_URL}/rebalance/plan")
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        await update.message.reply_text(f"❌ Failed to get plan: {e}")
        return

    plan_text = f"📊 **Rebalance Plan**\nTotal value: ${data['total_value_usd']}\n\n"
    for t in data["plan"]:
        plan_text += f"{t['action'].upper()} {t['amount_base']} {t['asset']} (~${t['amount_usd']})\n"

    if not data["plan"]:
        await update.message.reply_text("✅ Portfolio is already balanced!")
        return

    keyboard = [
        [
            InlineKeyboardButton("✅ Confirm & Execute", callback_data="confirm"),
            InlineKeyboardButton("❌ Cancel", callback_data="cancel"),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
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
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(f"{FASTAPI_URL}/rebalance/execute")
                resp.raise_for_status()
                result = resp.json()
        except Exception as e:
            await query.edit_message_text(f"❌ Execute failed: {e}")
            return

        result_text = "\n".join(result.get("results", ["No details returned"]))
        await query.edit_message_text(f"🚀 Rebalance executed!\n\n{result_text}")


def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("rebalance", rebalance_command))
    app.add_handler(CallbackQueryHandler(button_callback))

    print("🤖 Telegram bot starting (polling)...")
    app.run_polling()


if __name__ == "__main__":
    main()
