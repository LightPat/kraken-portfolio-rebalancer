from fastapi import FastAPI, HTTPException, Depends, Security
from fastapi.security import APIKeyHeader
from pydantic import BaseModel
from typing import List, Dict, Any
import os
from rebalancer import generate_rebalance_plan, execute_trades
from sheets import update_current_allocations_in_sheet

API_KEY_NAME = "X-API-Key"
api_key_header = APIKeyHeader(name=API_KEY_NAME, auto_error=False)


async def get_api_key(api_key: str = Security(api_key_header)):
    """Simple header-based API key check. Uses env var REBALANCER_API_KEY."""
    expected_key = os.getenv("REBALANCER_API_KEY")
    if not expected_key:
        raise HTTPException(
            status_code=500, detail="Server misconfigured - no API key set"
        )
    if api_key is None or api_key != expected_key:
        raise HTTPException(status_code=403, detail="Invalid or missing API key")
    return api_key


app = FastAPI(
    title="Kraken Portfolio Rebalancer API",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


class PlanResponse(BaseModel):
    total_value_usd: float
    current_portfolio: dict
    plan: list
    quote_currency: str
    dry_run: bool


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get(
    "/rebalance/plan", response_model=PlanResponse, dependencies=[Depends(get_api_key)]
)
async def get_plan():
    return generate_rebalance_plan()


@app.post("/updateCurrentAllocations", dependencies=[Depends(get_api_key)])
async def update_current_allocations():
    return update_current_allocations_in_sheet()


@app.post("/rebalance/execute", dependencies=[Depends(get_api_key)])
async def execute_rebalance():
    """Generate a fresh rebalance plan and execute the trades. Always uses the latest data from Google Sheets + Kraken."""
    # 1. Get the latest plan (same as /rebalance/plan)
    plan_data = generate_rebalance_plan()
    trade_plan = plan_data.get("plan", [])

    # 2. Early return if nothing to do
    if not trade_plan:
        return {
            "status": "already_balanced",
            "message": "Portfolio is already balanced according to targets.",
            "dry_run": plan_data["dry_run"],
            "total_value_usd": plan_data["total_value_usd"],
            "quote_currency": plan_data["quote_currency"],
            "plan_data": plan_data,  # for debugging/logging
        }

    # 3. Execute the trades
    results = execute_trades(trade_plan)

    # 4. Return clear success info
    return {
        "status": "executed",
        "message": "Rebalance trades executed successfully.",
        "results": results,
        "dry_run": plan_data["dry_run"],
        "total_value_usd": plan_data["total_value_usd"],
        "executed_plan": trade_plan,
        "plan_data": plan_data,
    }
