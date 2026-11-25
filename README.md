# kraken-portfolio-rebalancer

A bot that rebalances my kraken crypto portfolio based on a google sheet and executes when a telegram bot receives a message from me.

app.py -> FastAPI backend (API server)
bot.py -> Telegram bot (triggers the rebalance via /rebalance and /cancel_rebalance)
rebalancer.py -> Core logic (generate plan + execute trades with sequential sell/buy ordering, timeouts, and market fallback)
sheets.py -> Reads target allocations from Google Sheet
kraken.py -> Kraken exchange wrapper (via ccxt)

In the Signals sheet, set Column B by hand to `Long` or `Short`; Column C is a
positive percentage of capital. Telegram signal updates still write `Long`.
Shorts use 2x spot margin (Kraken's minimum leverage): a 25% short reserves 25%
of capital and opens twice that amount in notional. The Kraken API key must have
margin trading permission. Shorts incur Kraken's opening fee plus the 4-hour
rollover fee.