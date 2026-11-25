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
Set `TREZOR_BTC_PUB_KEY` in `.env` to the Trezor account-level Bitcoin xpub,
ypub, or zpub. The bot derives receive addresses using the prefix-appropriate
BIP44/49/84 format and sums confirmed plus unconfirmed balances from
mempool.space, stopping after 20 consecutive unused addresses. An unset or
empty key is treated as a zero cold-wallet balance.

SDCA trades are manual and require Telegram confirmation:

- `/sdca` reads the signed decimal in Signals!Q4. Positive values buy that
  fraction of Q3 (USD Left); negative values sell that fraction of total SDCA
  BTC (Q7 + Q8). The bot sells only free BTC on Kraken and reports how much
  more BTC must be moved from cold storage when the target exceeds the Kraken
  balance.

After an SDCA buy, the bot reports the filled BTC amount for manual withdrawal
to cold storage. `/updateCurrentAllocations` refreshes the SDCA balance cells
and records RSPS BTC quantity in E5.
