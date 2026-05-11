# kraken-portfolio-rebalancer

A bot that rebalances my kraken crypto portfolio based on a google sheet and executes when a telegram bot receives a message from me.

app.py -> FastAPI backend (API server)
bot.py -> Telegram bot (triggers the rebalance via /rebalance)
rebalancer.py -> Core logic (generate plan + execute trades)
sheets.py -> Reads target allocations from Google Sheet
kraken.py -> Kraken exchange wrapper (via ccxt)