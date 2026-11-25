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

The Signals sheet splits account equity between RSPS (N2) and SDCA (N3).
RSPS sizing uses its split less the RSPS-only reserve in I3. SDCA uses its own
USD cash pool in Q3 and BTC tracked in Q7 (Kraken) and Q8 (cold storage).
Set `BTC_COLD_ADDRESS` in `.env` to one or more comma-separated public Bitcoin
addresses; balances are read from mempool.space.

SDCA trades are manual and require Telegram confirmation:

- `/sdcaBuy` plans a buy using Q4 × Q3.
- `/sdcaSell 25` plans to sell 25% of SDCA's BTC held on Kraken. BTC in cold
  storage is never sold by the bot.

After an SDCA buy, the bot reports the filled BTC amount for manual withdrawal
to cold storage. `/updateCurrentAllocations` refreshes the SDCA balance cells
and records RSPS BTC quantity in E5.