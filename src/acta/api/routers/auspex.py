"""Auspex: preset analytical questions answered over the user's own data."""

import datetime
import re
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from acta.config import TZ
from acta.insights import auspex, explore

router = APIRouter()


class AuspexAsk(BaseModel):
    question_id: str
    date: Optional[str] = None
    month: Optional[str] = None


@router.get("/api/auspex/questions")
def auspex_questions():
    """The preset list. There is deliberately no free-text endpoint: every
    question is bound to a builder that assembles exactly the facts it needs,
    which is what keeps the model away from data it cannot reason about."""
    return {"questions": [{"id": q["id"], "text": q["text"], "group": q["group"],
                           "params": q["params"]} for q in auspex.QUESTIONS]}


@router.get("/api/auspex/today")
def auspex_today():
    """Answers since the last wake. Older ones stay in auspex_query and stop
    being rendered -- see auspex.day_start_ms for why the boundary is waking
    rather than midnight."""
    return auspex.visible()


@router.post("/api/auspex/explore")
def auspex_explore():
    """Phase 3: the model proposes pairings worth testing, Python tests every one
    against the same gates lag_mining uses, and only what survives is narrated.
    Two model calls, so slower and roughly double the cost of a preset question."""
    out = explore.run()
    if out.get("status") != "ok":
        raise HTTPException(502, out.get("error") or "explore failed")
    return {"id": out.get("id"), "question_id": "explore",
            "question_text": "Propose and test new hypotheses about my data",
            "answer": out["answer"], "provider": out["provider"],
            "model": auspex.LLM_MODEL, "cost_usd": out["cost_usd"],
            "latency_ms": out["latency_ms"], "n_supported": out["n_supported"],
            "created_at": datetime.datetime.now(TZ).isoformat(timespec="seconds")}


@router.post("/api/auspex/ask")
def auspex_ask(body: AuspexAsk):
    """Synchronous on purpose. The call takes 5-20s, but this is a single-user
    dashboard and a polling job would be more moving parts than the wait is
    worth; the frontend shows a progress state instead."""
    if body.question_id not in auspex.BY_ID:
        raise HTTPException(400, f"unknown question '{body.question_id}'")
    if body.date and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", body.date):
        raise HTTPException(400, "date must be YYYY-MM-DD")
    if body.month and not re.fullmatch(r"\d{4}-\d{2}", body.month):
        raise HTTPException(400, "month must be YYYY-MM")
    row = auspex.ask(body.question_id, date=body.date, month=body.month)
    if row["status"] != "ok":
        # The failure is already logged with its context, so it stays auditable
        # even though the caller only sees the message.
        raise HTTPException(502, row["error"] or "model call failed")
    return {k: row.get(k) for k in
            ("id", "ts", "question_id", "question_text", "answer", "provider",
             "model", "cost_usd", "latency_ms", "created_at")}
