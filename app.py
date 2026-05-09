from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import List, Dict, Any
import os
from .rebalancer import generate_rebalance_plan, execute_trades

app = FastAPI(title="Kraken Portfolio Rebalancer API")


class PlanResponse(BaseModel):
    total_value_usd: float
    current_portfolio: dict
    plan: list
    quote_currency: str
    dry_run: bool


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/rebalance/plan", response_model=PlanResponse)
async def get_plan():
    return generate_rebalance_plan()


@app.post("/rebalance/execute")
async def execute(plan: List[Dict[str, Any]]):
    if not plan:
        raise HTTPException(400, "Empty plan")
    results = execute_trades(plan)
    return {
        "results": results,
        "dry_run": os.getenv("DRY_RUN", "false").lower() == "true",
    }
