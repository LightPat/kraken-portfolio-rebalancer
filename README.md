# kraken-portfolio-rebalancer

app.py -> FastAPI backend (your API server)
bot.py -> Telegram bot (triggers the rebalance via /rebalance)
rebalancer.py -> Core logic (generate plan + execute trades)
sheets.py -> Reads target allocations from Google Sheet
kraken.py -> Kraken exchange wrapper (via ccxt)