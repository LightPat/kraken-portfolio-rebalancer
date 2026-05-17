from fastapi import FastAPI, HTTPException, Depends, Security
from fastapi.security import APIKeyHeader
from pydantic import BaseModel
import os
import time
from pathlib import Path
from filelock import FileLock, Timeout
from rebalancer import (
    generate_rebalance_plan,
    execute_trades,
    request_cancel_rebalance,
    reset_cancel_rebalance,
)
from sheets import update_current_allocations_in_sheet
from kraken import cancel_open_orders

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


REBALANCE_LOCK_FILE = "rebalance.lock"
REBALANCE_LOCK_TIMEOUT_SECONDS = 1

rebalance_lock = FileLock(REBALANCE_LOCK_FILE)

app = FastAPI(
    title="Kraken Portfolio Rebalancer API",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


class PlanResponse(BaseModel):
    total_value_usd: float
    desired_reserve: float
    investable_value: float
    current_stables_total: float
    current_portfolio: dict
    plan: list
    dry_run: bool


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/updateCurrentAllocations", dependencies=[Depends(get_api_key)])
async def update_current_allocations():
    return update_current_allocations_in_sheet()


@app.get(
    "/rebalance/plan", response_model=PlanResponse, dependencies=[Depends(get_api_key)]
)
async def get_plan():
    return generate_rebalance_plan()


@app.post("/rebalance/execute", dependencies=[Depends(get_api_key)])
async def execute_rebalance():
    """Generate a fresh rebalance plan and execute the trades. Always uses the latest data from Google Sheets + Kraken."""
    try:
        with rebalance_lock.acquire(timeout=REBALANCE_LOCK_TIMEOUT_SECONDS):
            reset_cancel_rebalance()

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
                    "plan_data": plan_data,
                }

            results = execute_trades(trade_plan)
            return {
                "status": "executed",
                "message": "Rebalance trades executed successfully.",
                "results": results,
                "total_value_usd": plan_data["total_value_usd"],
                "desired_reserve": plan_data["desired_reserve"],
                "investable_value": plan_data["investable_value"],
                "current_stables_total": plan_data["current_stables_total"],
                "dry_run": plan_data["dry_run"],
                "executed_plan": trade_plan,
                "plan_data": plan_data,
            }
    except Timeout:
        age = None
        try:
            lock_path = Path(REBALANCE_LOCK_FILE)
            if lock_path.exists():
                age = int(time.time() - lock_path.stat().st_mtime)
        except Exception:
            age = None
        message = "A rebalance is already running. Please try again later."
        if age is not None:
            message += f" (lock file age: {age}s)"
        raise HTTPException(status_code=409, detail=message)


@app.post("/rebalance/cancel", dependencies=[Depends(get_api_key)])
async def cancel_rebalance():
    request_cancel_rebalance()
    try:
        cancelled = cancel_open_orders()
        return {
            "status": "cancel_requested",
            "message": "Requested rebalance cancellation and cancelled open orders.",
            "cancelled_order_ids": cancelled,
        }
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Failed to cancel open orders: {e}"
        )
