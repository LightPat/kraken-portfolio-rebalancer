import math
import os
import re
import httpx
from sheets import parse_sdca_signal_update
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
EXECUTE_TIMEOUT_SECONDS = 1200.0
CONNECT_TIMEOUT_SECONDS = 10.0
CRYPTO_DECIMALS = 6
USD_DECIMALS = 2
PRICE_DECIMALS = 4


def parse_sdca_override(value: str) -> float:
    """Convert a decimal fraction or percentage input to a signed fraction."""
    raw_value = value.strip()
    has_percent_sign = raw_value.endswith("%")
    numeric_text = raw_value[:-1].strip() if has_percent_sign else raw_value
    try:
        numeric_value = float(numeric_text)
    except ValueError as exc:
        raise ValueError("Enter one signed percentage or decimal fraction.") from exc
    if not math.isfinite(numeric_value):
        raise ValueError("The SDCA change must be a finite number.")

    change_pct = (
        numeric_value / 100
        if has_percent_sign or abs(numeric_value) > 1
        else numeric_value
    )
    if abs(change_pct) > 1:
        raise ValueError("SDCA Change % Today must be between -100% and 100%.")
    return change_pct


def detect_signal_type(text: str) -> str | None:
    sdca_signal = re.search(r"\bSDCA\s+Signal\s+Update\b", text, re.IGNORECASE)
    rsps_signal = re.search(
        r"\bPortfolio\s+Signal\s+Update\b", text, re.IGNORECASE
    )
    if sdca_signal and rsps_signal:
        return "ambiguous"
    if sdca_signal:
        return "sdca"
    if rsps_signal:
        return "rsps"
    return None


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 Kraken Rebalancer ready!\n\nCommands:\n"
        "/rebalance\n/sdca\n/sdcaSet <value>\n\n/updateCurrentAllocations"
    )


async def sdca_set_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER_ID:
        await update.message.reply_text("❌ Unauthorized")
        return
    if len(context.args) != 1:
        await update.message.reply_text(
            "Usage: /sdcaSet <signed percentage or decimal fraction>"
        )
        return

    try:
        change_pct = parse_sdca_override(context.args[0])
    except ValueError as exc:
        await update.message.reply_text(f"❌ {exc} No sheet changes were made.")
        return

    status_message = await update.message.reply_text(
        "🔄 Updating SDCA Change % Today..."
    )
    try:
        timeout = httpx.Timeout(HTTP_TIMEOUT_SECONDS, connect=CONNECT_TIMEOUT_SECONDS)
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                f"{FASTAPI_URL}/updateSdcaChange",
                json={"change_pct": change_pct},
                headers={"X-API-Key": REBALANCER_API_KEY},
            )
            resp.raise_for_status()
    except Exception as exc:
        await status_message.edit_text(
            f"❌ SDCA override failed: {type(exc).__name__}: {exc}"
        )
        return

    direction = "buy" if change_pct > 0 else "sell" if change_pct < 0 else "no change"
    await status_message.edit_text(
        f"✅ SDCA Change % Today set to {abs(change_pct) * 100:.2f}% "
        f"({direction})."
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


async def sdca_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER_ID:
        await update.message.reply_text("❌ Unauthorized")
        return
    if context.args:
        await update.message.reply_text("Usage: /sdca")
        return

    status_message = await update.message.reply_text(
        "🔄 Reading SDCA Change % Today and preparing a plan..."
    )
    try:
        timeout = httpx.Timeout(HTTP_TIMEOUT_SECONDS, connect=CONNECT_TIMEOUT_SECONDS)
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(
                f"{FASTAPI_URL}/sdca/plan",
                headers={"X-API-Key": REBALANCER_API_KEY},
            )
            resp.raise_for_status()
            plan = resp.json()
    except Exception as e:
        await status_message.edit_text(
            f"❌ Failed to get SDCA plan: {type(e).__name__}: {e}"
        )
        return

    if plan["action"] is None or plan["amount_base"] <= 0:
        await status_message.edit_text(
            "ℹ️ SDCA Change % Today is zero; there is nothing to do."
        )
        return

    action = plan["action"]
    description = (
        f"📊 SDCA {action.upper()} Plan\n"
        f"• BTC amount: {plan['amount_base']:.8f}\n"
        f"• Approximate value: ${plan['amount_usd']:.2f}\n"
        f"• USD Left: ${plan['cash_usd']:.2f}\n"
        f"• BTC on Kraken: {plan['kraken_btc']:.8f}\n"
        f"• Total SDCA BTC: {plan['kraken_btc'] + plan['cold_btc']:.8f}\n"
        f"DRY_RUN = {plan['dry_run']}\n\n"
        "Execute this SDCA trade?"
    )
    if action == "sell" and plan["unavailable_btc"] > 0:
        description += (
            f"\n📥 Additional BTC to move from cold storage: "
            f"{plan['unavailable_btc']:.8f}"
        )
    callback_data = "sdca_confirm"
    keyboard = [[
        InlineKeyboardButton("✅ Execute", callback_data=callback_data),
        InlineKeyboardButton("❌ Cancel", callback_data="sdca_cancel"),
    ]]
    await status_message.edit_text(
        description, reply_markup=InlineKeyboardMarkup(keyboard)
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

    if query.data == "sdca_cancel":
        await query.edit_message_text("❌ SDCA trade cancelled.")
        return

    if query.data == "sdca_confirm":
        await query.edit_message_text("🔄 SDCA trade in progress...")
        try:
            timeout = httpx.Timeout(
                EXECUTE_TIMEOUT_SECONDS, connect=CONNECT_TIMEOUT_SECONDS
            )
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(
                    f"{FASTAPI_URL}/sdca/execute",
                    headers={"X-API-Key": REBALANCER_API_KEY},
                )
                resp.raise_for_status()
                result = resp.json()
        except Exception as e:
            await query.edit_message_text(
                f"❌ SDCA execute failed: {type(e).__name__}: {e}"
            )
            return

        result_text = "\n".join(result.get("results", ["No details returned"]))
        await query.edit_message_text(f"🚀 SDCA trade complete!\n\n{result_text}")
        return

    if query.data == "confirm":
        await query.edit_message_text("🔄 Rebalance in progress...")
        try:
            timeout = httpx.Timeout(
                EXECUTE_TIMEOUT_SECONDS, connect=CONNECT_TIMEOUT_SECONDS
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
    """Route explicitly titled SDCA and RSPS signal updates."""
    if update.effective_user.id != ALLOWED_USER_ID:
        return

    text = (update.message.text or update.message.caption or "").strip()
    signal_type = detect_signal_type(text)
    if signal_type is None:
        return
    if signal_type == "ambiguous":
        await update.message.reply_text(
            "❌ Signal message contains both SDCA and Portfolio update titles; "
            "no sheet changes were made."
        )
        return

    if signal_type == "sdca":
        try:
            change_pct = parse_sdca_signal_update(text)
        except ValueError as e:
            await update.message.reply_text(
                f"❌ SDCA signal parse failed: {e}. No sheet changes were made."
            )
            return

        status_message = await update.message.reply_text(
            "🔄 Updating SDCA Change % Today..."
        )
        try:
            timeout = httpx.Timeout(
                HTTP_TIMEOUT_SECONDS, connect=CONNECT_TIMEOUT_SECONDS
            )
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(
                    f"{FASTAPI_URL}/updateSdcaChange",
                    json={"change_pct": change_pct},
                    headers={"X-API-Key": REBALANCER_API_KEY},
                )
                resp.raise_for_status()
        except Exception as e:
            await status_message.edit_text(
                f"❌ SDCA signal update failed: {type(e).__name__}: {e}"
            )
            return

        direction = "buy" if change_pct > 0 else "sell" if change_pct < 0 else "no change"
        await status_message.edit_text(
            f"✅ SDCA Change % Today set to {abs(change_pct) * 100:.2f}% "
            f"({direction})."
        )
        return

    if "RSPS Signal:" not in text:
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
    app.add_handler(CommandHandler("sdca", sdca_command))
    app.add_handler(CommandHandler("sdcaSet", sdca_set_command))
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
