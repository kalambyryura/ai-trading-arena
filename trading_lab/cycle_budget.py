"""Read-only accounting shared by the panel and the user-operated strategy."""

from contextlib import closing
from decimal import Decimal, InvalidOperation
import sqlite3

MAX_CYCLES = 20
MAX_DECISIONS = 11
SERIES_ATTEMPTS = MAX_CYCLES * MAX_DECISIONS
SERIES_COST = Decimal("0.05")
CYCLE_COST = Decimal("0.005")
LEDGER_NAME = "jev_position_v2.sqlite3"


def web_ledgers(root):
    folder = root / "runtime" / "web_runs"
    return list(folder.glob("*/runtime/jev_one_trade_v1.sqlite3")) + list(folder.glob("*/runtime/" + LEDGER_NAME))


def ledger_usage(paths):
    attempts, cost, unknown = 0, Decimal(0), False
    for path in paths:
        try:
            with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
                rows = db.execute("SELECT status,reported_cost FROM attempts").fetchall()
        except sqlite3.Error:
            unknown = True  # Broken accounting blocks new spending, never the Stop UI.
            continue
        attempts += len(rows)
        for status, paid in rows:
            try:
                value = Decimal(str(paid))
            except (InvalidOperation, ValueError):
                value = Decimal("NaN")
            if status != "accounted" or not value.is_finite() or value < 0:
                unknown = True
            else:
                cost += value
    return {"attempts": attempts, "cost_usd": str(cost), "unresolved": unknown}


def series_available(root):
    usage = ledger_usage(web_ledgers(root))
    return not usage["unresolved"] and usage["attempts"] < SERIES_ATTEMPTS and Decimal(usage["cost_usd"]) < SERIES_COST
