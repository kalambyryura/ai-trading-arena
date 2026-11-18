"""Owner-started OKX Demo/isolated Live seasons. Opening the server does no trading.

All side effects run only after start/resume. Network calls never hold the state
lock, so an owner Stop can durably cancel an approval while Jev/OKX is pending.
"""
from contextlib import ExitStack, contextmanager
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import threading
import time

from .arena import IDENTITY, exclusive, read, write
from .arena_engine import emit, frame, iso
from . import strategy_runtime as runtime
from . import strategy_v4_feed as v4feed
from .exchange_book import GapBookFeed
from . import strategy_v4_runtime as v4rt
from .ops_policy import ClockGuard, RollingRequestBudget
from .ops_storage import HealthState, StorageBlocked
from . import exchange_protection as protection
from .arena_jev import ArenaJevLedger, SharedJevGate, CONFIDENCE_THRESHOLD
from .arena_strategies import ArenaError, fingerprint, number
from .exchange_accounts import profiles, public_profiles
from . import live_leverage  # R2.1.15: outside every core hash, like live_denylist
from .exchange_api import (ExchangeAPI, ExchangeFeed, TERMINAL, DEMO_FUNDING_WARNING, market_diagnostic, OrderAbsent, OrderRejected,
                           CancelFailed, CancelRefused)
from . import exchange_engine as engine
from . import demo_execution
from . import exchange_execution
from . import exchange_diagnostics
from .exchange_clock import ExchangeClock, POLICY as CLOCK_POLICY
from .okx_readonly import PreflightError, instrument_summary, decimal_value as D
from .strategy_contract import payload_of, recipe_intact
from .cycle_probe import is_probe, check_summary, TIMING
from .match_protocol import (MAX_DURATION_SECONDS, close_admission, draining_policy,
                             install, public_phase, validate_manifest)
from .match_journal import (append_events, checkpoint, drop_writer, journal_copy, restore_events, compact_memory,
                            require_normal_capacity)

CONTEXTS = {"workshop_demo": "demo", "arena_live": "live"}
POLICIES = {
    "workshop_demo": {"season_attempts": 61, "total_attempts": 500, "season_soft_usd": .01,
                      "total_soft_usd": .01, "maximum_answer_age_seconds": 8, "request_headroom_usd": .0005},
    "arena_live": {"season_attempts": 61, "total_attempts": 2000, "season_soft_usd": 1.,
                   "total_soft_usd": 1., "maximum_answer_age_seconds": 8, "request_headroom_usd": .0005},
}
LEDGERS = {"workshop_demo": "workshop_jev_e038.sqlite3", "arena_live": "arena_jev_e039.sqlite3"}
# An entry order needs fresh account checks, then a fresh quote taken after them, that confirm the same signal: the
# account checks finished at most 10 s before the quote was requested (an account check is timed at its finish; its
# position read comes a few requests earlier) and the quote is at most 10 s old at the last check before the order is
# written (a later candidate of the tick gets a new quote, and a new account check if needed, after the previous order).
# The decision snapshot itself is older by the tick's account checks: Live 2026-10-30, 1898 ticks: median 6.0 s,
# p95 9.9 s, max 15 s; OKX Demo answers slower, ~2-3 s a request, ~25 s a check. Without a model the recheck decides
# again on the fresh quote, so the decision snapshot only has a stall guard. A paid shared Jev answer is about its own
# snapshot: the call needs it at most 10 s old and the order at most 30 s, so no answer is bought or used stale.
FRESH_QUOTE_MAX_AGE_SECONDS = 10
ACCOUNT_CHECK_MAX_AGE_SECONDS = 10
ACCOUNT_CHECK_HARD_MAX_AGE_SECONDS = 30  # at the order itself, whatever slowed the quote request down
PAID_DECISION_MAX_AGE_SECONDS = 10
JEV_DECISION_MAX_AGE_SECONDS = 30
RULES_DECISION_MAX_AGE_SECONDS = 120
EXTENSION_SECONDS = 120  # A3 (BOARD 2def374): the most a run of transient codes keeps a protected position open
# R2.1.14 (R2113-STOP-FIRED-LAG, D160, D161): our own stop fired between its GET and the position GET of one account check;
# when the venue does not show it fired yet, the bot waits at most this many checks or seconds (clock), whichever comes first
STOP_FIRE_WAIT_CHECKS, STOP_FIRE_WAIT_SECONDS = 4, 60
EXTENSION_STOP_AGE_SECONDS = 60  # A3: the owned stop matched on a GET at most this long before the run's first failure
DEGRADED_WINDOWS_KEPT = 2000  # R2.1.11 (A4): the record in the state is bounded; the events keep every window
VERDICT_GRACE_SECONDS = 60  # R2110-STUCK: the most the one extra check for a two-phase verdict may take
STEP_TIMING_EVERY = 360  # R2.1.10 (C): a step_timing event about every hour at 10 s a step
FILL_WATCH_SECONDS = 1  # while a post-only entry rests, its fills are read every second and get the owned stop at once
PROTECTION_TARGET_SECONDS = 5  # journalled target from a venue fill to its verified owned stop (fill_protected)
BILL_FIELDS = ("billId", "ts", "type", "subType", "ccy", "instId", "ordId", "pnl", "fee", "balChg", "posBalChg", "bal",
               "sz", "px", "execType", "fillTime", "tradeId")  # kept as read, for an independent reconciliation
BILLS_PER_EVENT = 100  # well inside one journal row (an allowlisted bill is ~400 B)
# R2.1.5: a sent order whose POST answer was uncertain and that the venue never shows (51603) is concluded not created
# only after this many reads over this many seconds, with no fill, not pending and the position equal to own fills.
ABSENT_READS, ABSENT_SECONDS = 3, 60
# R2.1.6: an order recorded as not created is read again when the position differs from own fills, for as long as OKX
# keeps a cancelled unfilled order ("The incomplete orders that have been canceled are only reserved for 2 hours").
NOT_CREATED_RECHECK_SECONDS = 7200


def fresh_market(snapshot, now):
    """A3: this step's public snapshot arrived and its quote is at most FRESH_QUOTE_MAX_AGE_SECONDS old."""
    return bool(snapshot) and bool(snapshot.get("quote_ts")) and now - snapshot["quote_ts"] / 1000 <= FRESH_QUOTE_MAX_AGE_SECONDS


def exit_facts(bot, recipe, snapshot, stamp, reason):
    """R2.1.10 (C): what the exit compared, new keys only. Live compares the raw touch (bid for a long, ask for a short),
    the evaluator its modelled bid x 0.9998 (D136 p. 4: documented, not aligned); one account check per step."""
    facts = {"hold_clock": "monotonic" if "монотонний" in reason else "venue_fill_time"} if "Час утримання" in reason else {}
    rules = recipe["rules"]
    if snapshot and bot["position_btc"]:
        long = bot["position_btc"] > 0
        facts.update(compare_price=snapshot["bid"] if long else snapshot["ask"], price_basis="bid" if long else "ask",
                     gain_bps=engine.gain_bps(bot, snapshot), stop_loss_bps=rules["stop_loss_bps"],
                     take_profit_bps=rules["take_profit_bps"],
                     quote_age_seconds=round(stamp - snapshot["quote_ts"] / 1000, 3) if snapshot.get("quote_ts") else None)
    if bot["position_btc"] and rules.get("max_hold_seconds") is not None and bot.get("_hold_start") is not None:
        facts.update(max_hold_seconds=rules["max_hold_seconds"],
                     hold_deadline_ms=int((bot["_hold_start"] + rules["max_hold_seconds"]) * 1000),  # from the venue's fill time
                     hold_overrun_seconds=round(stamp - bot["_hold_start"] - rules["max_hold_seconds"], 3))
    return facts


def permission_tokens(check):
    """D-1: the key's permissions beyond Read+Trade as fixed tokens; preflight already refuses unknown scopes."""
    return sorted({p for p in check.get("unused_permissions") or [] if p == "withdraw"})


DEMO_PERMISSION_WARNING = "Увага: ключ Demo має зайве право Withdraw. Для Live такий ключ не пройде перевірку; потрібен ключ лише Read+Trade."


ORDER_BODY_FIELDS = ("instId", "tdMode", "posSide", "side", "ordType", "sz", "px", "reduceOnly", "clOrdId")


def execution_profile(state, recovery):
    """R2.1.10 (C): how this run executes, written at every Start and Resume (a new event)."""
    return {"exit_price_basis": "touch", "model_exit_slippage_bps": 2, "account_checks_per_step": 1,
            "poll_seconds": state["rules"].get("poll_seconds", engine.LIMITS["poll_seconds"]),
            "hosted_stop": state.get("protocol_version", 0) >= 8 and not any(is_probe(r) for r in state["strategies"]),
            "hosted_stop_trigger": "last", "hosted_stop_order": "market", "hosted_stop_cancel_on_close": True,
            "hold_clock": "venue_fill_time", "hold_backup_clock": "monotonic",
            "bounded_extension": state.get("protocol_version", 0) >= 8 and not any(is_probe(r) for r in state["strategies"]),
            "test_live_max_lots": state["rules"].get("test_live_max_lots"), "resumed": bool(recovery),
            "transient_codes_checked_at": exchange_diagnostics.OKX_ERROR_CHECKED_AT,
            "code_sha256": LOADED_HASH}  # R2.1.11 (R2110-HASH-COVER): the code of the process of this Start or Resume


def order_body(body):
    """R2.1.10 (C): the order's wire fields under a new, exported key; clOrdId is built from the public match id."""
    return {k: body[k] for k in ORDER_BODY_FIELDS if k in body}


def uncertain_send(order):
    """The POST of this order may or may not have reached the venue: an uncertain answer, or no answer recorded."""
    return bool(order.get("ack_uncertain")) or "post_answered_at" not in order
CANCEL_RETRY_SECONDS = 5  # a post-only order rests until cancelled: its cancel is repeated until a GET shows it terminal
MAX_CANCEL_ATTEMPTS = 12  # per worker run: a Resume starts a fresh allowance, the journal keeps the total


def tag_snapshot(snapshot, contract):
    """R2.1.12 (MIX, plan check p. 3): a mixed season's snapshot names its contract beside it (arena_market's snapshot has
    no such field and is under the hash); the ticker must be that contract's."""
    if ((snapshot.get("raw") or {}).get("ticker") or [{}])[0].get("instId") != contract:
        raise PreflightError("ticker_instrument_mismatch")
    snapshot["_instrument"] = contract
    return snapshot


def own_snapshot(state, bot, snapshot):
    """R2.1.12 (MIX, plan check p. 3): in a mixed season a bot is marked, judged and sized on its own contract's
    snapshot only; ticker_instrument_mismatch (manual review) otherwise. A one-contract season: unchanged."""
    if snapshot is not None and engine.mixed(state) and snapshot.get("_instrument") != engine.instrument_for(state, bot["id"]):
        raise PreflightError("ticker_instrument_mismatch")
    return snapshot


def last_snapshot(state, bot):
    """R2.1.12 (MIX): the last accepted snapshot of the bot's own contract in a mixed season, its tag checked."""
    return own_snapshot(state, bot, (state.get("_last_by_instrument") or {}).get(engine.instrument_for(state, bot["id"])))


@contextmanager
def instrument_block(state, bot):
    """R2.1.12 (MIX, D149 p. 3, plan check p. 5): while the bot's contract has an entry block, the bot sees it as the
    season's own, so the V2 runtime (strategy_runtime, under the arena hash) refuses its entries as for a season block; the
    other contract's bots never see it. exchange_engine reads the block itself (engine.contract_block, R2.1.13 X1-09); this
    is left only around runtime.advance: in the worker thread under the lock, and advance saves nothing."""
    reason = ((state.get("admission_blocked_by_instrument") or {}).get(engine.instrument_for(state, bot["id"]))
              if engine.mixed(state) and not state.get("admission_blocked") else None)
    if reason:
        state["admission_blocked"] = reason
    try:
        yield
    finally:
        if reason and state.get("admission_blocked") == reason:
            state.pop("admission_blocked")


# R2.1.15 (plan check p. 8): OKX bills subtypes of a forced liquidation (100-107, the transfers 110-111) and of ADL (125-128)
LIQUIDATION_SUBTYPES = frozenset({*map(str, range(100, 108)), "110", "111"})
ADL_SUBTYPES = frozenset(map(str, range(125, 129)))


def leverage_plan(context, execution_mode, recipes, requested):
    """R2.1.15 (D168 p. 3, D178): the leverage of each bot at a Start, checked before any request. None: every bot 1x, as
    an older client asks. Each a whole number from 1 to its version's limit (live_leverage); a recipe the table does not
    list, Demo (D189, LL-03), the Demo mechanics and the technical plan E041 trade 1x (D181 p. 2: no Demo at 3x)."""
    if requested is None:
        return [1] * len(recipes), [1] * len(recipes)
    if (not isinstance(requested, list) or len(requested) != len(recipes)
            or any(type(v) is not int for v in requested)):
        raise ArenaError("Плече: одне ціле число для кожного вибраного бота.")
    tops = []
    for recipe, lever in zip(recipes, requested):
        top = (1 if context == "workshop_demo" or execution_mode == demo_execution.MODE or is_probe(recipe)
               else live_leverage.limit(recipe.get("recipe_sha256")))  # D189 (LL-03): Demo 1x only, as the gates' model
        if not 1 <= lever <= top:
            name = (live_leverage.LEVERAGE_LIMITS.get(recipe.get("recipe_sha256")) or (None, recipe.get("name") or recipe.get("id")))[1]
            raise ArenaError(f"Плече {lever}x не дозволене для {name}: межа версії {top}x.")
        tops.append(top)
    return list(requested), tops


def venue_price(value):
    """A positive OKX number, or None for an empty or malformed field (liqPx may be empty: plan check p. 1)."""
    try:
        return D(value, positive=True)
    except PreflightError:
        return None


# RC2 (LL-04): the liquidation price's source, worst to best; model_1x_long (D189): a 1x long, never liquidated
FACT_RANK = {None: 0, "computed": 1, "model_1x_long": 1, "okx": 2}


def leveraged(state):
    """D189 (LL-02): a round with the manifest's leverage block (any bot above 1x): its liquidation facts, tiers and
    liquidation reads are for every bot of it, 1x included. 1x rounds have none of them, byte for byte as before."""
    return isinstance((state.get("match_manifest") or {}).get("leverage"), dict)


def computed_liquidation(bot, contracts, avg, margin):
    """R2.1.15 (D181 p. 2; L1's official OKX formula for isolated USDT-margined swaps): the liquidation price from the entry
    price, the position's margin (else the notional over the leverage) and the tier's mmr plus the bot's taker fee:
    long (Q*avg - M) / (Q*(1 - m)), short (Q*avg + M) / (Q*(1 + m)). (price, tier) or (None, None) without a tier."""
    size = abs(contracts)
    tier = next((t for t in bot.get("_tiers") or [] if D(t["min_contracts"]) <= size <= D(t["max_contracts"])), None)
    if tier is None or not avg or not size:
        return None, None
    base = size * engine.contract_btc(bot)
    margin = margin or base * avg / engine.leverage(bot)
    m = D(tier["mmr"]) + abs(D(bot["_fee_rate"]))
    price = (base * avg - margin) / (base * (1 - m)) if contracts > 0 else (base * avg + margin) / (base * (1 + m))
    tick = D(bot["_instrument"]["tick_size"])
    return ((price / tick).to_integral_value() * tick if price > 0 else D("0")), tier  # 1x long: never liquidated


def unseen_receipt(error):
    """R2.1.15 (D183 p. 3, QA-03): OKX's 51603 ("order does not exist") on the GET of an owned stop's receipt."""
    return (getattr(error, "okx_code", None) == "51603" and getattr(error, "method", None) == "GET"
            and getattr(error, "endpoint", None) == protection.STOP_RECEIPT)


def venue_sum(rows, key, *, signed=False):
    """The sum of one numeric bills field (absolute values unless signed); None if any row lacks a number."""
    total = D("0")
    for row in rows:
        try:
            value = D(row.get(key))
        except PreflightError:
            return None
        total += value if signed else abs(value)
    return engine.N(total)


def entry_blockers(state, bot, candidate, stamp):
    """Why exchange_engine.candidate refused a recheck: the failed recipe checks, else the season/bot state it gates on."""
    if any(c.get("passed") is not True for c in candidate["checks"]):
        return ["local_checks_failed"]
    found = [name for name, blocked in (
        ("owner_stop", bot["stop_requested"]), ("season_not_running", state["status"] != "RUNNING"),
        ("admission_ended", stamp >= state["_end"]), ("unresolved_order", bool(engine.unresolved(bot))),
        ("manual_review", bot["_manual_review"]), ("admission_blocked", bool(state.get("admission_blocked") or engine.contract_block(state, bot) or bot.get("admission_blocked") or bot.get("entries_blocked"))),
        ("protection_unverified", bool(bot.get("protection")) and bot["protection"].get("verified") is not True)) if blocked]
    return found or ["recipe_refused"]  # e.g. a declarative recipe's bar or reservation guard
ENTRY_CANCELLED = {
    "season_not_running": "Сезон не приймає входів: ордера немає.",
    "admission_ended": "Вікно входів закрито: ордера немає.",
    "season_deadline": "Сезон завершується: ордера немає.",
    "decision_snapshot_too_old": "Рішення спирається на надто старий знімок: ордера немає.",
    "decision_too_slow": "Рішення надійшло пізніше 8 с: ордера немає.",
    "fresh_snapshot_failed": "Свіжу котировку не отримано: ордера немає.",
    "owner_stop": "Stop власника: ордера немає.",
}


SOURCE_FILES = ("ikigai_g1.py", "ikigai_g1_reference.py", "causal_htf.py", "exchange_lab.py", "exchange_clock.py", "exchange_engine.py",
                "demo_execution.py", "exchange_execution.py", "exchange_diagnostics.py", "exchange_api.py", "exchange_accounts.py", "arena_jev.py",
                "arena_market.py", "arena_engine.py", "arena_strategies.py", "strategy_v2.py", "strategy_v4.py", "strategy_v4_book.py", "strategy_v4_feed.py", "strategy_v4_runtime.py", "strategy_runtime.py",
                "ops_policy.py", "ops_storage.py", "cycle_probe.py", "cycle_diagnostics.py", "match_protocol.py", "match_journal.py",
                "strategy_contract.py", "exchange_protection.py", "jev.py", "risk.py", "okx_readonly.py", "exchange_book.py")


def source_hash(folder=None):
    """sha256 over the exchange sources with only CRLF -> LF normalised (D098)."""
    folder = Path(folder or Path(__file__).parent)
    return hashlib.sha256(b"".join((folder / name).read_bytes().replace(b"\r\n", b"\n") for name in SOURCE_FILES)).hexdigest()


LOADED_HASH = source_hash()


def intent_message(body):
    return ("Ордер закриття зарезервовано до POST; повторно не надсилається." if body["reduceOnly"]
            else "Ордер зарезервовано до POST; цей вхід не повторюється.")


def winner_name(state):
    """By the season's score policy: since filled_entry_net_v1 the cash reference never wins and a strategy needs a
    filled entry order (a strategy that lost still wins if it is the only one with an entry; a loss is not a profit)."""
    from .match_protocol import score_policy
    policy = score_policy(state)
    pool = [b for b in state["bots"] if b["id"] != "cash" or policy.get("include_cash", True)]
    if not policy.get("include_cash", True):
        pool = [b for b in pool if b.get("filled_entry_orders", 0) >= policy.get("minimum_entry_filled_orders", 1)]
    if not pool:
        return None
    top = max(b["pnl_usdt"] for b in pool)
    return " / ".join(b["name"] for b in pool if abs(b["pnl_usdt"] - top) < 1e-8)


def maker_evidence(maker, snapshot, fresh):
    """A post-only entry's plan as decided (the bar's close, tick, limit, fill threshold, expiry) beside the fresh quote it
    was placed against, so "the limit did not cross the book" is read from the journal, not reconstructed."""
    if not maker:
        return {}
    try:
        close = next(float(r[4]) for r in snapshot["raw"]["candles"] if int(r[0]) + 60000 == maker["bar_close_ms"])
    except (KeyError, TypeError, ValueError, StopIteration):
        close = None
    return {"maker_plan": {**deepcopy(maker), "decision_close": close},
            "fresh_quote": {"bid": fresh.get("bid"), "ask": fresh.get("ask"), "quote_ts": fresh.get("quote_ts")}}


class ExchangeLab:
    def __init__(self, root, workshop, arena, *, api_factory=None, feed_factory=None, jev_factory=None,
                 profile_provider=profiles, clock=time.time, monotonic=None, background=True):
        self.root, self.workshop, self.arena = Path(root), workshop, arena
        self.path = self.root / "runtime" / "exchange_v1"
        self.manifest = self.path / "manifest.json"
        self.lock, self.wall_clock, self.background = threading.RLock(), clock, background
        self.monotonic = monotonic if monotonic is not None else lambda: time.monotonic()
        self.timebase = ExchangeClock(clock, lambda: self.monotonic())
        self.clock = self.timebase.now
        self.api_factory = api_factory or (lambda p: ExchangeAPI.from_profile(self.root, p))
        self.feed_factory = feed_factory or (lambda a: ExchangeFeed(a, clock=self.clock))
        self.jev_factory, self.profile_provider = jev_factory, profile_provider
        self.data = read(self.manifest, {"generation": 0, "runs": [], "checks": {}})
        self.sessions, self.workers, self.resources = {}, {}, {}
        self.event_positions, self.checkpoint_times = {}, {}
        self.history_cache = {}  # run id -> ((mtime_ns, size), history row): a finished run's checkpoint never changes
        self.ledgers = {c: ArenaJevLedger(self.root, relative_path="runtime/exchange_v1/" + LEDGERS[c], budget=POLICIES[c])
                        for c in CONTEXTS}
        self.controller_owner = exclusive(self.path / "manager.lock")
        self.controller_owner.__enter__()
        self.closed = False
        # Deliberately no worker, exchange transport or model call on construction/GET.

    def load(self, identity, *, reader=False):
        """reader=True (journal_copy): the files only, never the in-memory session, and nothing in the engine changes;
        the caller reads the journal itself."""
        if not isinstance(identity, str) or not IDENTITY.fullmatch(identity) or identity not in self.data["runs"]:
            raise ArenaError("Біржовий запуск не знайдено.")
        state = (None if reader else self.sessions.get(identity)) or read(self.path / (identity + ".json"))
        if not state or state.get("id") != identity or state.get("context") not in CONTEXTS or state.get("mode") != (
                "OKX_DEMO" if state["context"] == "workshop_demo" else "OKX_LIVE"):
            raise ArenaError("Пошкоджений біржовий журнал.")
        for recipe in state["strategies"]:
            if recipe["id"] != "cash" and not recipe_intact(recipe):
                raise ArenaError("Змінено рецепт збереженого запуску.")
        validate_manifest(state)
        if state.get("clock_policy") is not None and (state["clock_policy"] != CLOCK_POLICY
                or state.get("match_manifest", {}).get("clock_policy") != CLOCK_POLICY):
            raise ArenaError("Нечинна політика годинника збереженого запуску.")
        if state.get("execution_mode") == demo_execution.MODE:
            demo_execution.validate_state(state)
        elif state.get("execution_mode") == exchange_execution.RULES_ONLY:
            exchange_execution.validate_state(state)
        if draining_policy(state) and identity not in self.sessions and not reader:
            self.event_positions[identity] = restore_events(self.path / (identity + ".events.jsonl"), state)
        return state

    def running(self, identity):
        worker = self.workers.get(identity)
        return bool(worker and worker.is_alive()) or identity in self.resources

    def public(self, state, *, compact=False):
        interrupted = state["status"] in {"RUNNING", "DRAINING", "CLOSING"} and not self.running(state["id"])
        result = engine.public({**state, "status": "PAUSED"} if interrupted else state, compact=compact)
        if state.get("clock_policy") == CLOCK_POLICY:
            remaining = None
            if state["status"] in {"DONE", "DRAINING", "CLOSING"}:
                remaining = 0
            elif not interrupted and state["status"] == "RUNNING" and self.timebase.synchronized:
                resource = self.resources.get(state["id"])
                remaining = max(0, resource["deadline"] - self.monotonic() if resource else state["_end"] - self.clock())
            result["timing"] = {"source": CLOCK_POLICY, "admission_remaining_seconds": remaining,
                                "server_now_ms": self.clock() * 1000 if self.timebase.synchronized else None}
        coin = (state.get("rules") or {}).get("instrument", engine.LIMITS["instrument"]).split("-")[0]
        result["quantity_units"] = {"coin": coin, "note": "Поля *_btc містять кількість у монетах інструмента сезону ("
                                    + coin + "); result.contracts — контракти OKX."}
        if engine.mixed(state):  # R2.1.12 (MIX): no season coin; each bot's base_ccy names its own
            result["quantity_units"] = {"coin": None, "note": "Змішаний сезон: поля *_btc містять кількість у монетах інструмента "
                                        "кожного бота (його base_ccy); result.contracts — контракти OKX."}
        if interrupted:
            result.update(status="PAUSED", incomplete=True,
                          phase="PAUSED",
                          error="Процес зупинився. Доступне лише відновлення звірки та закриття; нових входів немає.")
        if (state["context"] == "workshop_demo" and len(state["strategies"]) == 2
                and state["strategies"][0].get("recipe_sha256") == demo_execution.RECIPE_SHA256):
            from .demo_v8_review import review
            result["demo_operating_review"] = review(state)
        return result

    def history(self, identity):
        return self.public(journal_copy(self, identity, receipt=True))  # the view of a copy needs no lock

    def history_row(self, state):
        return {**{k: deepcopy(state[k]) for k in ("id", "context", "mode", "created_at", "status", "budget_usdt",
                                                   "duration_seconds", "incomplete")},
                "participants": len(state["bots"]) - 1, "jev_usage": deepcopy(state.get("jev_usage")),
                "example_only": __import__("trading_lab.league", fromlist=["x"]).example_only(state),  # R2.1.11 (D141 p. 2)
                "execution_mode": state.get("execution_mode", "shared_jev")}

    def history_key(self, identity):
        """A finished run's history row is cached by its checkpoint's mtime and size: the first read is the full
        validated load (recipes, manifest, journal), and after that a poll no longer re-reads and re-parses every
        journal of the history, each of which holds thousands of events after a 5-hour battle."""
        if identity in self.sessions:
            return None
        try:
            stat = (self.path / (identity + ".json")).stat()
        except OSError:
            return None
        return stat.st_mtime_ns, stat.st_size

    def status(self):
        with self.lock:
            active, history = {c: None for c in CONTEXTS}, []
            for identity in reversed(self.data["runs"]):
                key, cached = self.history_key(identity), self.history_cache.get(identity)
                if key is not None and cached and cached[0] == key:
                    history.append(deepcopy(cached[1]))
                    continue
                state = self.load(identity)
                shown_status = "PAUSED" if state["status"] in {"RUNNING", "DRAINING", "CLOSING"} and not self.running(identity) else state["status"]
                history.append(self.history_row(state))
                history[-1]["status"] = shown_status
                if key is not None and state["status"] == "DONE":
                    self.history_cache[identity] = (key, deepcopy(history[-1]))
                if state["status"] != "DONE" and active[state["context"]] is None:
                    active[state["context"]] = self.public(state, compact=True)
            return {"api_version": 8, "market_check_version": 1, "cycle_probe_version": 2,
                    "decision_diagnostics_version": 2, "match_protocol_version": 8,
                    "duration_options": [300, 900, 1800, 3600, 7200, 18000, 86400],
                    "demo_execution_check": demo_execution.capability(),
                    "demo_execution_checks": demo_execution.capabilities(),
                    "ordinary_execution_modes": ["shared_jev", exchange_execution.RULES_ONLY],
                    "leverage_limits": live_leverage.limits(),  # R2.1.15: {recipe_sha256: limit} for the Start form
                    "rules_only_check": exchange_execution.capability(),
                    "generation": self.data["generation"],
                    "profiles": public_profiles(self.root, self.data["checks"]), "active": active, "history": history,
                    "budgets": {"workshop": self.ledgers["workshop_demo"].summary(""),
                                "arena": self.ledgers["arena_live"].summary("")}}

    def guard(self, generation):
        if self.closed or type(generation) is not int or generation != self.data["generation"]:
            raise ArenaError("Стан біржових запусків змінився. Онови панель.")

    def execution_owner_guard(self):
        marker = Path(self.root) / "runtime/server_handoff.json"
        if marker.exists() or marker.is_symlink():
            raise ArenaError("Цей workspace передано. Виконання дозволене лише на цільовій інсталяції; збережи handoff-маркер.")

    def save(self, state=None, *, force=True, recovery=None):
        if state is not None:
            identity = state["id"]
            if draining_policy(state):
                if recovery is None:
                    recovery = bool(state.get("_recovery") or any(
                        b.get("stop_requested") or b.get("_exit_requested") and b.get("_reduce_remaining") is None
                        for b in state["bots"] if b["id"] != "cash"))
                try:
                    self.event_positions[identity] = append_events(self.path / (identity + ".events.jsonl"), state,
                                                                  self.event_positions.get(identity, 0), recovery=recovery)
                    resource = self.resources.get(identity)
                    if resource and isinstance(state.get("ops_health"), dict):  # the checkpoint names what is durable now
                        # the appended events only: the checkpoint heartbeat stays with the step's own mark
                        resource["health"].last_durable_seq = max(resource["health"].last_durable_seq, self.event_positions[identity])
                        state["ops_health"]["last_durable_seq"] = resource["health"].last_durable_seq
                except (ArenaError, OSError):
                    drop_writer(self.path / (identity + ".events.jsonl"))
                    state.update(status="PAUSED", incomplete=True, admission_blocked="evidence_storage",
                                 error="Не вдалося надійно записати журнал; залишки збережені, потрібне відновлення власником.")
                    raise
                if (not force and self.monotonic() - self.checkpoint_times.get(identity, 0)
                        < state["rules"]["checkpoint_interval_seconds"]):
                    return
            write(self.path / (state["id"] + ".json"), checkpoint(state) if draining_policy(state) else state)
            self.checkpoint_times[identity] = self.monotonic()
            compact_memory(state)
        write(self.manifest, self.data)

    def chosen_profiles(self, account_ids):
        if (not isinstance(account_ids, list) or not 1 <= len(account_ids) <= 6
                or any(not isinstance(i, str) for i in account_ids) or len(set(account_ids)) != len(account_ids)):
            raise ArenaError("Потрібні унікальні профілі, по одному на бота.")
        known = {p["id"]: p for p in self.profile_provider(self.root)}
        if any(i not in known for i in account_ids):
            raise ArenaError("Профіль OKX не знайдено.")
        return [known[i] for i in account_ids]

    def check(self, generation, account_ids, strategy_ids=None):
        """strategy_ids (R2.1.12, MIX): the recipe of each account, so its contract and market are checked; without them BTC."""
        with self.lock:
            self.guard(generation)
            selected = self.chosen_profiles(account_ids)
            contracts = [engine.LIMITS["instrument"]] * len(selected)
            if strategy_ids is not None:
                if (not isinstance(strategy_ids, list) or len(strategy_ids) != len(selected)
                        or any(not isinstance(i, str) for i in strategy_ids)):
                    raise ArenaError("Перевір кількість стратегій і акаунтів.")
                with self.arena.lock:
                    known = {r["id"]: r for r in self.arena.catalogue() if r["status"] == "ready"}
                if any(i not in known for i in strategy_ids):
                    raise ArenaError("Потрібні готові незмінні рецепти з каталогу арени.")
                found = engine.bot_instruments([known[i] for i in strategy_ids])  # the function of the Start
                contracts = [found[i] for i in strategy_ids]
        results = {}
        for profile, contract in zip(selected, contracts):
            feed = None
            try:
                api = self.api_factory(profile)
                api.inst_id = contract
                checked = api.preflight(25, profile["environment"] == "live")
                extra = permission_tokens(checked)
                if extra and profile["environment"] == "live":
                    raise PreflightError("live_key_has_unused_permissions")  # D-1 (D136 p. 7): Read+Trade, no Withdraw
                feed = self.feed_factory(api)
                snapshot = feed.snapshot()
                warning = DEMO_FUNDING_WARNING if snapshot.get("funding_status") == "unknown_schedule" else ""
                results[profile["id"]] = {"ready": True, "instrument": contract,
                    "reason": "Акаунт, свіжі котирування та завершені свічки перевірені. " + warning,
                    "last_checked_at": iso(snapshot["server_time"]), "available_usdt": checked["balance"]["available"],
                    "is_subaccount": checked["is_subaccount"], "_account_tag": checked["account_tag"],
                    "unused_permissions": checked["unused_permissions"], "funding_status": snapshot.get("funding_status", "available"),
                    "market_source": snapshot.get("source"), "market_checked": True}
                if extra:  # Demo: a visible warning, the check still passes
                    results[profile["id"]].update(permission_warning=DEMO_PERMISSION_WARNING,
                                                  reason=results[profile["id"]]["reason"] + " " + DEMO_PERMISSION_WARNING)
            except (PreflightError, ArenaError, OSError, ValueError, KeyError, TypeError) as error:
                reason = "Перевір Read+Trade, Futures/net, USDT, відсутність позицій/ордерів та окремий subaccount для Live."
                if str(error) == "live_key_has_unused_permissions":
                    reason = exchange_diagnostics.MESSAGES[str(error)]
                if feed is not None:
                    reason = "Ринкові дані не пройшли перевірку: " + market_diagnostic(error, feed)["message"]
                results[profile["id"]] = {"ready": False, "instrument": contract, "reason": reason, "market_checked": False,
                                          "last_checked_at": iso(self.clock())}
        with self.lock:
            self.data["checks"].update(results)
            self.data["generation"] += 1
            self.save()
            return {"checked": account_ids, "generation": self.data["generation"],
                    "ready": all(r["ready"] for r in results.values())}

    def start(self, generation, context, strategy_ids, account_ids, budget_usdt, duration_seconds, execution_mode="shared_jev",
              leverage=None):
        with self.lock:
            self.execution_owner_guard()
            self.guard(generation)
            if context not in CONTEXTS or source_hash() != LOADED_HASH:
                raise ArenaError("Невідомий режим або код оновлено: потрібне оновлення панелі.")
            if execution_mode not in {"shared_jev", exchange_execution.RULES_ONLY, demo_execution.MODE} or (
                    execution_mode == demo_execution.MODE and context != "workshop_demo"):
                raise ArenaError("Невідомий execution_mode або Demo-механіка вибрана для Live.")
            budget = number(budget_usdt, 25, 500)
            duration = number(duration_seconds, 300, MAX_DURATION_SECONDS)
            if int(duration) != duration:
                raise ArenaError("Тривалість має бути цілою кількістю секунд.")
            selected = self.chosen_profiles(account_ids)
            if (not isinstance(strategy_ids, list) or len(strategy_ids) != len(selected)
                    or any(not isinstance(i, str) for i in strategy_ids) or len(set(strategy_ids)) != len(strategy_ids)
                    or budget * len(selected) > 1000 or (context == "workshop_demo" and len(selected) != 1)
                    or any(p["environment"] != CONTEXTS[context] for p in selected)):
                raise ArenaError("Перевір кількість стратегій, бюджет і окремі Demo/Live профілі.")
            for i in self.data["runs"]:
                prior = self.load(i)
                if prior["status"] != "DONE" and (prior["context"] == context or any(b.get("account_id") in account_ids for b in prior["bots"])):
                    raise ArenaError("Спершу заверши/віднови попередній запуск цих акаунтів.")
            if context == "workshop_demo":
                with self.workshop.lock:
                    recipes = [deepcopy(self.workshop.version(strategy_ids[0])["strategy"])]
            else:
                with self.arena.lock:
                    known = {r["id"]: r for r in self.arena.catalogue() if r["status"] == "ready"}
                    if any(i not in known for i in strategy_ids):
                        raise ArenaError("Потрібні готові незмінні рецепти з каталогу арени.")
                    recipes = [deepcopy(known[i]) for i in strategy_ids]
            if context == "arena_live":  # R2.1.11 (E): a fresh Live Start only; never a Resume or a Stop
                from .live_denylist import LIVE_START_DENIED
                retired = [LIVE_START_DENIED[r["recipe_sha256"]] for r in recipes if r.get("recipe_sha256") in LIVE_START_DENIED]
                if retired:
                    raise ArenaError("Рецепт знято з ужитку для Live (" + retired[0] + "): обери інший.")
            if any(not recipe_intact(r) for r in recipes):
                raise ArenaError("Хеш рецепта не збігається.")
            levers, tops = leverage_plan(context, execution_mode, recipes, leverage)  # R2.1.15: refused before any request
            engine.season_instruments(recipes, context, execution_mode)  # R2.1.12 (MIX): a mixed season only Live RULES_ONLY
            mechanics = exchange_execution.validate_request(execution_mode, context, recipes, budget, duration)
            rules_only = execution_mode == exchange_execution.RULES_ONLY
            if any(v4rt.enabled(r) for r in recipes) and not rules_only and not mechanics:
                raise ArenaError("Схема 4 виконується лише в режимі RULES_ONLY: Jev її входів не перевіряє.")
            usage = self.ledgers[context].summary("")
            if not mechanics and not rules_only and (usage["series_unknown_billing"] or usage["series_attempts"] >= POLICIES[context]["total_attempts"]
                    or usage["series_reported_cost_usd"] + POLICIES[context]["request_headroom_usd"] >= POLICIES[context]["total_soft_usd"]):
                raise ArenaError("Jev: сукупний бюджет використано або рахунок невідомий.")
            if any(is_probe(r) for r in recipes) and (context != "workshop_demo" or len(recipes) != 1
                    or budget != TIMING["budget_usdt"] or duration != TIMING["duration_seconds"]):
                raise ArenaError("Технічний план: тільки один OKX Demo бот, 100 USDT бюджету й 5 хвилин; покупки обмежені 10 + 10 USDT.")
        # Public GET only, outside the state lock. No private read, setting,
        # order or paid request precedes the durable run reservation below.
        try:
            clock_api = self.api_factory(selected[0])
            calibration = clock_api.calibrate_clock()
        except (PreflightError, OSError, ValueError, TypeError) as error:
            raise ArenaError(exchange_diagnostics.failure(error, stage="preflight")["message"]) from None
        with self.lock:
            self.execution_owner_guard()
            self.guard(generation)  # A concurrent owner action invalidates the prepared request.
            if source_hash() != LOADED_HASH:
                raise ArenaError("Код оновлено: потрібне оновлення панелі.")
            self.timebase.synchronize(clock_api.session_server_now(), replace=not any(self.running(i) for i in self.data["runs"]))
            state = engine.create(recipes, account_ids, context, budget, duration, self.clock(), LOADED_HASH, execution_mode)
            state["degraded_windows"] = []  # R2.1.11 (A4): only a journal started by R2.1.11 keeps and exports the record
            install(state, account_ids, None if mechanics or rules_only else POLICIES[context])
            state["clock_policy"] = CLOCK_POLICY
            state["match_manifest"]["clock_policy"] = CLOCK_POLICY
            if any(lever > 1 for lever in levers):
                # R2.1.15 (plan check p. 4): new keys only above 1x, so that 1x journals and their bundles stay byte for byte
                bots = {b["id"]: b for b in state["bots"]}
                for recipe, lever in zip(recipes, levers):
                    bots[recipe["id"]]["leverage"] = lever
                state["match_manifest"]["leverage"] = {
                    "table_sha256": live_leverage.table_sha256(), "margin_mode": "isolated",
                    "stop_notional_budget_pct": engine.STOP_NOTIONAL_BUDGET_PCT,
                    "bots": [{"strategy_id": r["id"], "recipe_sha256": r["recipe_sha256"], "leverage": lever, "limit": top}
                             for r, lever, top in zip(recipes, levers, tops)]}
            state["match_manifest_sha256"] = fingerprint(state["match_manifest"])
            emit(state, self.clock(), None, 1, "exchange_clock_synchronized",
                 "Час OKX перевірено. Тривалість відраховує монотонний таймер; годинник Windows не впливає.",
                 **calibration)
            if rules_only:
                state["match_manifest"]["shadow_policy"] = deepcopy(exchange_execution.SHADOW_POLICY)
                state["match_manifest"]["decision_policy"] = exchange_execution.capability()
                state["match_manifest_sha256"] = fingerprint(state["match_manifest"])
                exchange_execution.validate_state(state)
                emit(state, self.clock(), None, 2, "rules_only_mode",
                     "Власник вибрав RULES_ONLY: рішення за рецептом; запити Jev записуються для офлайн SHADOW.",
                     execution_mode=execution_mode, ai_used=False, shadow_policy=deepcopy(exchange_execution.SHADOW_POLICY))
            if mechanics:
                state["match_manifest"].update(evaluation="demo_execution_acceptance", repeat_cycles=False,
                                               demo_execution_check=demo_execution.capability(recipes[0]["recipe_sha256"]))
                state["match_manifest_sha256"] = fingerprint(state["match_manifest"])
                demo_execution.validate_state(state)
                emit(state, self.clock(), state["bots"][0], 2, "demo_execution_mode",
                     "Власник вибрав перевірку механіки OKX Demo без Jev. Не є тестом переваги стратегії.",
                     execution_mode=execution_mode, ai_used=False, recipe_sha256=recipes[0]["recipe_sha256"])
            # The first event pins the code and the final manifest; it is not durable before the save below.
            next(e for e in state["events"] if e["code"] == "exchange_reserved")["data"].update(
                code_sha256=state["code_sha256"], match_manifest_sha256=state["match_manifest_sha256"])
            # This reservation precedes thread creation, private requests, writes and model calls.
            self.sessions[state["id"]] = state
            self.data["runs"].append(state["id"])
            self.data["generation"] += 1
            self.save(state)
            self.launch(state, selected, False)
            return {"accepted": True, "id": state["id"], "mode": state["mode"], "execution_mode": execution_mode}

    def stop(self, generation, id, bot_id=None):
        with self.lock:
            self.execution_owner_guard()
            self.guard(generation)
            state = self.load(id)
            if state["status"] == "DONE":
                raise ArenaError("Запуск уже завершено.")
            if bot_id is not None and (not isinstance(bot_id, str) or bot_id == "cash"):
                raise ArenaError("Невідомий учасник.")
            engine.stop(state, self.clock(), bot_id, origin="owner")
            self.sessions[id] = state
            self.data["generation"] += 1
            self.save(state)
            if id in self.resources:
                self.resources[id]["wakeup"].set()
            if not self.running(id):
                # Same persistent journal; never a new entry or paid recovery request.
                self.launch(state, self.chosen_profiles([b["account_id"] for b in state["bots"] if b["id"] != "cash"]), True)
            return {"accepted": True, "id": id, "closing_confirmed": False}

    def resume(self, generation, id):
        with self.lock:
            self.execution_owner_guard()
            self.guard(generation)
            state = self.load(id)
            if state["status"] == "DONE" or self.running(id):
                raise ArenaError("Закриття вже завершено або процес працює.")
            engine.stop(state, self.clock(), reason="Відновлення після перерви: лише звірка й закриття.", origin="recovery", code="recovery")
            state.update(_recovery=True, incomplete=True, error=None, _closing_since=self.clock(), status="CLOSING")
            for bot in state["bots"]:
                if bot["id"] != "cash":
                    # R2.1.14 (R2113-RESUME-SETTLED, D163 p. 2): a bot that settled before the pause (STOPPED, accounting
                    # confirmed, not in the manual review) keeps its accounting; one read of its account proves it still flat
                    settled = bot["status"] == "STOPPED" and bot["accounting_reconciled"] and not bot["_manual_review"]
                    bot.update(_manual_review=False, _closing_since=self.clock(),
                               _resume_closes=bot.get("_resume_closes", 0) + engine.RESUME_CLOSE_RESERVE)
                    if settled:
                        bot["_settled_check"] = True
                    else:
                        bot["accounting_reconciled"] = False
            self.sessions[id] = state
            self.data["generation"] += 1
            self.save(state)
            self.launch(state, self.chosen_profiles([b["account_id"] for b in state["bots"] if b["id"] != "cash"]), True)
            return {"accepted": True, "id": id, "close_only": True}

    def launch(self, state, selected, recovery):
        self.execution_owner_guard()
        if self.background:
            thread = threading.Thread(target=self.worker, args=(state, selected, recovery), name="OKX-" + state["id"][:8], daemon=False)
            self.workers[state["id"]] = thread
            thread.start()
        # Tests can explicitly prepare/step injected fake APIs without a background thread.

    def prepare(self, state, selected, recovery):
        self.execution_owner_guard()
        stack, apis, checks = ExitStack(), [], []
        try:
            if recovery or not self.timebase.synchronized:
                clock_api = self.api_factory(selected[0])
                clock_api.calibrate_clock()
                with self.lock:
                    self.timebase.synchronize(clock_api.session_server_now(), replace=not bool(self.resources))
                    if recovery:
                        # Monotonic epochs cannot be reused after a process restart.
                        # Only this fresh close-only attempt gets a new 120s reserve.
                        state["_closing_since"] = self.clock()
                        for bot in state["bots"]:
                            if bot["id"] != "cash": bot["_closing_since"] = self.clock()
                            if bot.get("_degraded"):  # R2110-WINDOW-END: the last process's window ends with it
                                self.end_window(state, bot, "restarted")
            mechanics = state.get("execution_mode") == demo_execution.MODE
            rules_only = state.get("execution_mode") == exchange_execution.RULES_ONLY
            if rules_only:
                exchange_execution.validate_state(state)
            if mechanics:
                demo_execution.validate_state(state)
                if len(selected) != 1 or selected[0]["environment"] != "demo":
                    raise PreflightError("demo_mechanics_profile_required")
            instrument = state["rules"].get("instrument", engine.LIMITS["instrument"])  # journals before R2: BTC
            for index, profile in enumerate(selected):
                bot = state["bots"][index]
                contract = engine.instrument_for(state, bot["id"])  # R2.1.12 (MIX, plan check p. 2): from the rules
                api = self.api_factory(profile)
                api.inst_id = contract
                api.leverage = int(bot.get("leverage", 1))  # R2.1.15: every position read, setting and entry limit of this bot
                api.request_limit = state["rules"].get("max_api_requests_per_account", 10000)
                api.recovery_request_reserve = state["rules"].get("recovery_request_reserve", 4096)
                if state.get("protocol_version", 0) >= 8:
                    api.rolling_reads = RollingRequestBudget(limit=100, window_seconds=2, critical_headroom=10)
                if recovery and bot["_initialized"]:
                    api.calibrate_clock()
                    check = api.identity(state["context"] == "arena_live")
                    if check["account_tag"] != bot["_account_tag"] or check["main_tag"] != bot["_main_tag"]:
                        raise PreflightError("recovery_account_changed")
                    api.instrument = instrument_summary(api.get("/api/v5/account/instruments",
                        {"instType": "SWAP", "instId": contract}, private=True), "swap", contract)
                    if api.instrument != bot["_instrument"] or (engine.mixed(state) and api.instrument.get("instrument") != contract):
                        raise PreflightError("recovery_contract_changed")
                    check.update(balance=bot["_start_balance"], instrument=api.instrument,
                                 taker_signed_rate=bot["_fee_rate"], server_start_ms=bot["_begin_ms"])
                else:
                    check = api.preflight(state["budget_usdt"], state["context"] == "arena_live")
                    if not recovery and profile["environment"] == "live" and permission_tokens(check):
                        # D-1: a fresh Start only. Never a Resume or a recovery: an open Live position must stay closable.
                        raise PreflightError("live_key_has_unused_permissions")
                if any(c["account_tag"] == check["account_tag"] for c in checks):
                    raise PreflightError("two_keys_are_one_account")
                stack.enter_context(exclusive(self.path / "accounts" / (check["account_tag"] + ".lock")))
                checks.append(check); apis.append(api)
            if state["context"] == "arena_live" and (len({a.site for a in apis}) != 1 or len({c["main_tag"] for c in checks}) != 1):
                raise PreflightError("live_accounts_must_share_site_and_parent")
            with self.lock:
                for bot, check in zip(state["bots"], checks):
                    if not recovery or not bot["_initialized"]:
                        engine.initialize(bot, check)
                    if engine.mixed(state) and (bot.get("_instrument") or {}).get("instrument") != engine.instrument_for(state, bot["id"]):
                        raise PreflightError("instrument_unavailable_or_mismatched")  # R2.1.12 (MIX, plan check p. 2)
                    emit(state, self.clock(), bot, 1, "account_checked", "GET перевірки акаунта й контракту пройдено.",
                         account_id=bot["account_id"], unused_permissions=check["unused_permissions"],
                         extra_permissions=permission_tokens(check),  # R2.1.10: the exported tokens (a new key)
                         start_balance=dict(bot["_start_balance"]))  # the opening USDT balance the settlement compares with
                state.update(_ready=True, _recovery=recovery)
                self.save(state)
            for bot, api in zip(state["bots"], apis):
                state["_worker_stage"] = "execution_setup"
                api.arm(state["budget_usdt"], close_only=recovery)
                if state["rules"].get("test_live_max_lots") and v4rt.enabled(next(r for r in state["strategies"] if r["id"] == bot["id"])):
                    # G12: a second, independent check of a schema-4 bot's entry size
                    api.max_entry_units = D(bot["_instrument"]["minimum_order_units"]) * state["rules"]["test_live_max_lots"]
                if not recovery and not bot["stop_requested"] and self.clock() < state["_end"]:
                    if leveraged(state):  # D181 p. 2, D189: the tiers for a computed liquidation price, before the setting
                        tiers = api.position_tiers()
                        with self.lock:
                            bot["_tiers"] = tiers
                    # Stop during this settings request prevents all entries after it returns.
                    confirmed = api.set_leverage()
                    with self.lock:
                        emit(state, self.clock(), bot, 1, "leverage_confirmed", f"Плече {api.leverage}x isolated підтверджено GET leverage-info.",
                             **(confirmed or {}))
            with self.lock:
                emit(state, self.clock(), None, 1, "execution_profile", "Режим виконання цього запуску.",
                     **execution_profile(state, recovery))
                self.save(state)
            state["_worker_stage"] = "market_setup"
            gate = None if mechanics or rules_only else (self.jev_factory(state["context"], self.ledgers[state["context"]]) if self.jev_factory else
                    SharedJevGate(self.root, ledger=self.ledgers[state["context"]], mode=state["mode"], clock=self.clock))
            def record_request(payload, payload_hash, requested_ms, candidates):
                with self.lock:
                    emit(state, requested_ms / 1000, None, 2, "jev_request", "Точний структурований запит збережено до виклику Jev.",
                        payload=payload, payload_sha256=payload_hash, request_at_ms=requested_ms,
                        payload_wire_json=json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")),
                        payload_content_sha256=fingerprint(payload),
                        candidate_ids=[c["id"] for c in candidates],
                        snapshot_sha256=(state.get("_last") or {}).get("sha256"),
                        observed_at=(state.get("_last") or {}).get("at"), bar_time_ms=(state.get("_last") or {}).get("bar_close_ms"))
                    self.save(state)
            if gate is not None:
                gate.before_send = record_request
            pool = ThreadPoolExecutor(max_workers=len(apis), thread_name_prefix="OKX-GET")
            stack.callback(pool.shutdown, wait=True)
            # R2.1.12 (MIX, plan check pp. 7-8): a mixed season reads each contract through its own client and feed, in the
            # worker thread one after another, on one rolling budget (one IP) and OKX's 10/2 s for history-index-candles;
            # a one-contract season builds the one feed as before
            mixed = engine.mixed(state)
            shared = RollingRequestBudget(limit=20, window_seconds=2, critical_headroom=2) if mixed else None
            index_reads = RollingRequestBudget(limit=10, window_seconds=2, critical_headroom=2) if mixed else None
            feeds = {}
            for contract in engine.contracts(state):
                market_api = self.api_factory(selected[0])
                market_api.inst_id = contract or instrument
                market_api.request_limit = state["rules"].get("max_public_gets", 2000) + state["rules"].get("max_book_gets", 0) + 128
                if state.get("protocol_version", 0) >= 8:
                    market_api.rolling_reads = shared or RollingRequestBudget(limit=20, window_seconds=2, critical_headroom=2)
                market_api.index_reads = index_reads
                feed = self.feed_factory(market_api)
                own = [r for r in state["strategies"] if engine.instrument_for(state, r["id"]) == contract] if mixed else state["strategies"]
                book_specs = v4feed.specs_of(own)
                if book_specs:  # schema 4: the book reads public history through the same client, on its own budget
                    feed = GapBookFeed(feed, lambda path, params, market_api=market_api: market_api.get(path, params, private=False), book_specs,
                        budget=state["rules"]["book_gets_by_instrument"].get(contract) if mixed else state["rules"].get("max_book_gets"),
                        instrument=contract or instrument,
                        admitting=lambda contract=contract: state["status"] == "RUNNING" and not state.get("_recovery") and not state.get("admission_blocked")
                            and not (state.get("admission_blocked_by_instrument") or {}).get(contract))
                if hasattr(feed, "max_calls"):
                    feed.max_calls = float("inf") if state.get("protocol_version", 0) >= 8 else state["rules"].get("max_public_gets", 2000)
                feeds[contract] = feed
            self.resources[state["id"]] = {"stack": stack, "apis": apis, "feeds": feeds, "gate": gate,
                **({} if mixed else {"feed": feeds[None]}),
                **({"clock_guards": {c: ClockGuard() for c in feeds}} if mixed else {}),
                "pool": pool, "wakeup": threading.Event(), "closing_deadlines": {},
                "hold_deadlines": {},
                "clock_guard": ClockGuard(), "health": HealthState(),
                "hosted_protection": state.get("protocol_version", 0) >= 8 and not any(is_probe(r) for r in state["strategies"]),
                "deadline": self.monotonic() + max(0, state["_end"] - self.clock())}
        except Exception:
            stack.close()
            raise

    def worker(self, state, selected, recovery):
        try:
            state["_worker_stage"] = "preflight"
            self.prepare(state, selected, recovery)
            state["_worker_stage"] = "running"
            while state["status"] not in {"DONE", "PAUSED"}:
                self.step(state)
                # Short waits keep owner Stop responsive; bounded network calls cannot be killed mid-write.
                if state["status"] not in {"DONE", "PAUSED"}:
                    self.idle(state, self.resources[state["id"]],
                              1 if any(b["stop_requested"] or b.get("_exit_requested") for b in state["bots"] if b["id"] != "cash") else 5)
        except Exception as error:
            unwatched = False
            with self.lock:
                state["incomplete"] = True
                diagnostic = exchange_diagnostics.failure(error, stage=state.get("_worker_stage", "running"))
                state["error"] = diagnostic["message"] + " Журнал збережено; новий ордер автоматично не повторюється."
                if any(b["_orders"] for b in state["bots"]):
                    engine.stop(state, self.clock(), reason=state["error"], code="worker_failed")
                    state["status"] = "PAUSED"
                    unwatched = True
                else:
                    state.update(status="DONE", finished_at=iso(self.clock()))
                emit(state, self.clock(), None, 1, "exchange_error", state["error"],
                     reason=diagnostic["reason"], stage=diagnostic["stage"], **exchange_diagnostics.venue_facts(error))
                try:
                    self.save(state)
                except (ArenaError, OSError):
                    # A persistent disk/quota failure must not escape this
                    # handler or turn an unclosed position into DONE. The old
                    # durable checkpoint remains authoritative on restart.
                    state.update(status="PAUSED", incomplete=True, admission_blocked="evidence_storage",
                                 error="Журнал недоступний; потрібна ручна звірка власного залишку.")
            if unwatched:  # network I/O never under the state lock; cancel_resting itself never raises
                resource = self.resources.get(state["id"])
                for bot, api in zip(state["bots"], resource["apis"] if resource else []):
                    try:
                        self.cancel_resting(state, bot, api, "worker_failed", final=True)
                    except Exception:  # noqa: BLE001 - one bot's unexpected fault never costs the next bot its cancel
                        pass
        finally:
            resource = self.resources.pop(state["id"], None)
            if resource:
                resource["stack"].close()
            if state["status"] == "DONE" and state["context"] == "workshop_demo":
                try:
                    self.workshop.record_exchange_trial(state)
                except (OSError, ValueError, KeyError, ArenaError):
                    with self.lock:
                        emit(state, self.clock(), None, 6, "report_pending", "Біржовий журнал збережено; звіт майстерні потребує повторного читання.")
                        self.save(state)

    def idle(self, state, resource, seconds):
        """Wait for the next step in FILL_WATCH_SECONDS slices; an owner action still wakes it at once. While a post-only
        entry rests, each slice reads its fills (watch_fills), so a passive fill is protected within about a second plus
        the venue's answer times instead of at the next full account check."""
        deadline = self.monotonic() + seconds  # the step cadence stays the same however slow the watch reads are
        for _ in range(max(1, int(seconds / FILL_WATCH_SECONDS))):
            remaining = deadline - self.monotonic()
            if remaining <= 0 or resource["wakeup"].wait(min(FILL_WATCH_SECONDS, remaining)):
                break
            if self.monotonic() < deadline and any(o["request"]["ordType"] == "post_only" and not o["request"]["reduceOnly"]
                   for bot in state["bots"] if bot["id"] != "cash" for o in engine.unresolved(bot)):
                self.watch_fills(state, "watch_slice")
        resource["wakeup"].clear()

    def read_resting(self, state, bot, api, seen_by):
        """One order GET per resting post-only entry of this bot; a new fill gets the owned stop at once, and an entry
        that may no longer rest (expiry, Stop, exit) is cancelled after that, never before."""
        resource = self.resources[state["id"]]
        filled, read = False, []
        for order in engine.unresolved(bot):
            if order["request"]["ordType"] != "post_only" or order["request"]["reduceOnly"]:
                continue
            known = deepcopy(order.get("result"))
            result = self.order_facts(state, bot, api, order)
            if result is None:
                continue  # not visible on the venue yet: the full account check decides
            with self.lock:  # account checks run in pool threads: a state write never races another bot's checkpoint
                order["_resting_reads"] = order.get("_resting_reads", 0) + 1
                if result != known:  # nothing new: no checkpoint write every second while the order rests
                    engine.record_result(state, bot, order, result, self.clock(), seen_by=seen_by)
                    self.save(state)
            filled = filled or D(result["contracts"]) > D((known or {}).get("contracts", "0"))
            read.append((order, result))
        if filled and resource.get("hosted_protection"):
            protection.reconcile(self, state, bot, api)  # a triggered stop's fill first, as checked_account does
            protection.ensure(self, state, bot, api)
            self.note_protection(state, bot)
        for order, result in read:  # the stop first, then an expiry within a slice instead of at the next step
            self.cancel_if_due(state, bot, api, order, result)

    def watch_fills(self, state, seen_by="watch_slice"):
        """read_resting for every bot between steps (watch_slice) and at the end of a step (step_end). The next step
        still runs the full account, position and pending checks. A failure is an event, never a pause: the full check
        decides."""
        self.execution_owner_guard()
        resource = self.resources[state["id"]]
        for bot, api in zip(state["bots"], resource["apis"]):
            if bot["status"] == "STOPPED" or bot["_manual_review"]:
                continue
            try:
                self.read_resting(state, bot, api, seen_by)
            except (PreflightError, ArenaError, OSError, ValueError, KeyError, TypeError) as error:
                with self.lock:
                    diagnostic = exchange_diagnostics.failure(error, stage="account_check", fallback="exchange_check_failed")
                    emit(state, self.clock(), bot, 4, "fill_watch_failed", "Швидке читання заповнення не вдалося; рішення — за повною перевіркою.",
                         reason=diagnostic["reason"], **exchange_diagnostics.venue_facts(error))
                    self.save(state)

    def note_protection(self, state, bot):
        """Journal how long an actual entry fill stood without the owned stop: from the venue's fill time and from our
        observation to the verified receipt that covers it; once per newly covered quantity of an order."""
        guard = bot.get("protection") or {}
        if (guard.get("status") != "ARMED" or guard.get("verified") is not True
                or D(guard.get("covered_contracts") or "0") != abs(D(bot["_contracts"])) or not D(bot["_contracts"])):
            return
        with self.lock:
            for order in bot["_orders"]:
                filled = D((order.get("result") or {}).get("contracts", "0"))
                if (order["request"]["reduceOnly"] or order.get("_protective_parent") or not filled
                        or D(order.get("protected_contracts", "0")) >= filled):
                    continue
                order["protected_contracts"] = str(filled)
                if "last_fill_venue_at" not in order:
                    continue  # a fill journalled before R2.1 has no venue time: no latency to report
                from_venue = round(guard["verified_at"] - order["last_fill_venue_at"], 3)
                stops = [str((r.get("receipt") or {}).get("uTime", "")) for r in bot.get("_protections", [])
                         if (r.get("proof") or {}).get("live_verified")]
                fill_ms = str(order["result"].get("updated_ms", ""))
                # R2.1.10 (C): the same interval on the venue's clocks alone (the stop's uTime, the fill's), a record only
                venue = ({"venue_stop_after_fill_ms": int(stops[-1]) - int(fill_ms)}
                         if stops and stops[-1].isdigit() and fill_ms.isdigit() else {})
                emit(state, self.clock(), bot, 4, "fill_protected", "Фактичне заповнення входу покрите власним stop на біржі.", **venue,
                     client_order_id=order["request"]["clOrdId"], contracts=str(filled), seconds_from_venue_fill=from_venue,
                     seconds_from_observation=round(guard["verified_at"] - order["last_fill_observed_at"], 3),
                     target_seconds=PROTECTION_TARGET_SECONDS, within_target=from_venue <= PROTECTION_TARGET_SECONDS,
                     seen_by=order.get("last_fill_seen_by"), resting_reads=order.get("_resting_reads", 0))
            try:
                self.save(state)
            except (ArenaError, OSError, StorageBlocked):
                pass  # a journal note only: the step's own save records it or pauses on the storage fault

    def cancel_if_due(self, state, bot, api, order, result):
        """Cancel a non-terminal own order that may not rest: an IOC remainder once (the venue ends it by itself), a
        post-only entry at its expiry or on Stop/exit/recovery, repeated (bounded) until a GET shows it terminal. Used by
        the account check and, for resting entries, by the one-second watch, so an expiry is cancelled within a slice."""
        resting = order["request"]["ordType"] == "post_only" and not order["request"]["reduceOnly"]
        if (result["state"] in TERMINAL or order.get("_protective_parent")
                or engine.keeps_resting(state, bot, order, self.clock(), api.close_only)):
            return
        if order.get("cancel_requested") and not (resting and self.cancel_due(state, order)):
            return
        with self.lock:
            if resting and self.clock() >= order.get("expires_at", float("inf")) and not order.get("expired"):
                order["expired"] = True
                bot["_next_decision"] = max(bot.get("_next_decision", 0), order["expires_at"])  # D096: a bar visible from the expiry on
                emit(state, self.clock(), bot, 3, "entry_order_expired",
                     "Тайм-аут post-only заявки: скасовуємо залишок; фактичні часткові заповнення лишаються власними.",
                     client_order_id=order["request"]["clOrdId"], contracts=result["contracts"])
            self.count_cancel(state, order)
            self.save(state)
        sent, outcome, failure = self.clock(), "acknowledged", None
        try:
            api.cancel(order["request"]["clOrdId"])
        except (PreflightError, OSError, ValueError) as error:  # a cancel that races a fill or the venue's own cancel: the GET decides
            outcome, failure = "uncertain", error
        with self.lock:
            emit(state, self.clock(), bot, 3 if not order["request"]["reduceOnly"] else 6, "exchange_cancel_posted",
                 "POST скасування заявки: час надсилання й відповіді біржі.", client_order_id=order["request"]["clOrdId"],
                 outcome=outcome, post_seconds=round(self.clock() - sent, 3))
        if outcome == "uncertain":
            result = self.order_facts(state, bot, api, order)
            if result is None:
                return  # a cancel of an order the venue does not show: the full account check decides
            with self.lock:
                engine.record_result(state, bot, order, result, self.clock(), seen_by="cancel_check")
                if result["state"] not in TERMINAL and resting:
                    emit(state, self.clock(), bot, 3, "entry_order_cancel_failed",
                         "Скасування post-only заявки не підтверджено; повторимо на наступній перевірці.",
                         client_order_id=order["request"]["clOrdId"], attempts=order["cancel_attempts"])
                self.save(state)
            if result["state"] not in TERMINAL and not resting:
                raise failure  # the original code (network_error, okx_error_*) reaches the journal

    def checked_account(self, state, bot, api):
        self.execution_owner_guard()
        # A resting post-only entry is read first: a fill since the last read gets its stop before the identity, pending
        # and position GETs of this check (seven or more requests that used to come before it).
        self.read_resting(state, bot, api, "account_check")
        identity = api.identity(state["context"] == "arena_live")
        if identity["account_tag"] != bot["_account_tag"] or identity["main_tag"] != bot["_main_tag"]:
            raise PreflightError("account_identity_changed")
        hosted = self.resources[state["id"]].get("hosted_protection")
        if hosted:
            try:
                protection.reconcile(self, state, bot, api)
            except PreflightError as error:
                self.stop_receipt_unseen(state, bot, error)  # QA-03: raises stop_fire_pending inside an open wait
                raise
        allowed = [o["request"]["clOrdId"] for o in bot["_orders"] if not o.get("_protective_parent")]
        if hosted:
            # Child IDs are admitted only after the parent's GET receipt and the child
            # instrument/side/reduce-only/quantity checks in reconcile above.
            children = [o["_child_id"] for o in bot["_orders"] if o.get("_protective_parent") and o.get("_child_id")]
            pending = api.pending(allowed, [p["proof"]["algo_id"] for p in bot.get("_protections", []) if p.get("proof")]
                                  + [p["ack_algo_id"] for p in bot.get("_protections", []) if p.get("ack_algo_id")], children, bot["_orders"])
        else: pending = api.pending(allowed, owned_orders=bot["_orders"])
        listed = {row.get("clOrdId") for row in pending if isinstance(row, dict)}
        for order in bot["_orders"]:  # R2.1.6: the venue lists an order recorded as not created: it exists after all
            if order.get("not_created") and order["request"]["clOrdId"] in listed:
                self.reopen(state, bot, order, "pending")
            elif (order.get("_absent") or {}).get("nothing_at") and order["request"]["clOrdId"] in listed:
                with self.lock:  # R219-RELIST: listed between the two phases: the verdict needs a fresh "nothing to cancel"
                    order["_absent"].pop("nothing_at")
                    self.save(state)
        for order in engine.unresolved(bot):
            result = self.order_facts(state, bot, api, order)
            if result is None:
                continue
            with self.lock:
                if order["request"]["ordType"] == "post_only" and not order["request"]["reduceOnly"]:
                    order["_resting_reads"] = order.get("_resting_reads", 0) + 1
                engine.record_result(state, bot, order, result, self.clock(), seen_by="account_check")
                self.save(state)
            self.cancel_if_due(state, bot, api, order, result)
        position = api.position()
        for _ in range(2):  # a resting post-only order may fill between its GET and the position GET: read its facts again
            if position == D(bot["_contracts"]) or not any(o["request"]["ordType"] == "post_only" for o in engine.unresolved(bot)):
                break
            for order in engine.unresolved(bot):
                result = self.order_facts(state, bot, api, order)
                if result is None:
                    continue
                with self.lock:
                    if order["request"]["ordType"] == "post_only" and not order["request"]["reduceOnly"]:
                        order["_resting_reads"] = order.get("_resting_reads", 0) + 1
                    engine.record_result(state, bot, order, result, self.clock(), seen_by="account_check")
                    self.save(state)
            position = api.position()
        if position != D(bot["_contracts"]) and self.adopt_hidden(state, bot, api):
            position = api.position()
        lagging = position != D(bot["_contracts"])
        unseen = lagging and self.unseen_explains(state, bot, position)  # facts of sent orders not visible yet: not foreign
        if hosted and lagging and not unseen:  # R2.1.14: our own stop may have fired during this check
            position = self.stop_fired(state, bot, api, position)
            lagging = position != D(bot["_contracts"])
        elif hosted and not lagging and bot.get("_stop_fire_wait"):
            self.stop_fire_end(state, bot)  # RC2 (RC-01): resolved only with the facts of the fire, else the manual review
        if lagging and not unseen and (not engine.fill_read_lag(bot, position) or bot.get("_fill_read_lags", 0) >= 2):
            raise PreflightError("actual_position_differs_from_owned_fills")  # a lag lasts one check; a third is not a lag
        if (lagging and not unseen) or bot.get("_fill_read_lags"):
            with self.lock:
                bot["_fill_read_lags"] = bot.get("_fill_read_lags", 0) + 1 if lagging and not unseen else 0
                if lagging and not unseen:
                    emit(state, self.clock(), bot, 3, "fill_read_lag",
                         "Позиція вже містить заповнення post-only заявки, якого ще не видно в її GET; перечитаємо на наступній перевірці.",
                         position_contracts=str(position), owned_contracts=bot["_contracts"])
                self.save(state)
        if not lagging:
            self.conclude_absent(state, bot, api, {row.get("clOrdId") for row in pending if isinstance(row, dict)})
            if leveraged(state):  # D189 (LL-02): every bot of the round, 1x included
                self.position_facts(state, bot, api, position)
        if hosted:
            try:
                protection.ensure(self, state, bot, api, own_fire=True)
            except PreflightError as error:
                if str(error) != "protective_fired_after_position_read":
                    raise
                self.stop_fired_late(state, bot, api)  # R2.1.14 (D164): the race's second window
            self.note_protection(state, bot)
        self.resources[state["id"]]["health"].mark("account", self.monotonic())
        self.resources[state["id"]]["health"].mark("protection", self.monotonic())
        if self.clock() >= bot.get("_next_funding_read", 0):
            bills = api.bills(bot["_begin_ms"])
            funding = engine.funding_total(bot, bills)
            with self.lock:
                if D(bot["_funding"]) != funding:
                    bot["_funding"] = str(funding)
                    emit(state, self.clock(), bot, 4, "exchange_funding", "Фактичний funding прочитано з OKX bills.",
                         signed_funding_usdt=str(funding), source=state["mode"])
                bot["_next_funding_read"] = self.clock() + 30
                self.save(state, force=False)
        return position

    def submit(self, state, bot, api, body, stamp):
        with self.lock:
            self.execution_owner_guard()
            recovery_write = bool(body["reduceOnly"] and (api.close_only or bot["stop_requested"]
                                  or bot.get("_exit_requested") and bot.get("_reduce_remaining") is None))
            if state.get("execution_mode") == demo_execution.MODE and not body["reduceOnly"]:
                demo_execution.validate_state(state)
                if (api.environment != "demo" or bot["_entry_attempts"] != 0
                        or body.get("side") != ("buy" if demo_execution.entry_side(state) == "long" else "sell")):
                    raise PreflightError("demo_mechanics_entry_scope")
            elif state.get("execution_mode") == exchange_execution.RULES_ONLY and not body["reduceOnly"]:
                exchange_execution.validate_state(state)
            closing_now = body["reduceOnly"] and (bot["stop_requested"] or state.get("_recovery"))
            if engine.intent_count(bot) >= engine.LIMITS["max_order_intents_per_bot"] + (bot.get("_resume_closes", 0) if closing_now else 0):
                raise PreflightError("order_intent_limit")  # R2.1.8: a Resume's close reserve is for Stop/recovery closes only
            if not body["reduceOnly"] and engine.intent_count(bot) >= engine.LIMITS["max_order_intents_per_bot"] - 20:
                raise PreflightError("close_intent_reserve")
            if body["reduceOnly"] and bot.get("_reduce_remaining") is not None and not bot["stop_requested"] and engine.intent_count(bot) >= engine.LIMITS["max_order_intents_per_bot"] - 20:
                raise PreflightError("discretionary_reduction_close_reserve")
            if not body["reduceOnly"] and (bot["stop_requested"] or state["status"] != "RUNNING"
                    or self.clock() >= state["_end"] or self.monotonic() >= self.resources[state["id"]]["deadline"]):
                return False
            engine.enforce_probe_order(state, bot, body)
            if not recovery_write:
                proposed = {"seq": state.get("_event_seq", 0) + 1, "at": iso(stamp), "bot_id": bot["id"],
                            "step": 6 if body["reduceOnly"] else 3, "code": "exchange_order_intent",
                            "message": intent_message(body),
                            "data": {"request": body, "order_body": order_body(body)}}
                try:
                    require_normal_capacity(self.path / (state["id"] + ".events.jsonl"), state, proposed)
                except (ArenaError, OSError):
                    state.update(incomplete=True, admission_blocked="evidence_storage")
                    raise  # No order, runtime reservation or entry counter was changed.
            order = {"request": deepcopy(body), "at": stamp, "send_started": True, "result": None}
            if body["reduceOnly"] and bot.get("_reduce_remaining") is not None and not bot["stop_requested"]:
                order["_reduction"] = True
            recipe = next(r for r in state["strategies"] if r["id"] == bot["id"])
            if body["ordType"] == "post_only":  # D096: it rests until it fills or this expiry, then the rest is cancelled
                order["expires_at"] = stamp + recipe["rules"]["order"]["timeout_seconds"]
            if not body["reduceOnly"]:
                runtime.reserve(recipe, bot, last_snapshot(state, bot) if engine.mixed(state) else state["_last"], body["clOrdId"])
            bot["_orders"].append(order)
            if not body["reduceOnly"]:
                bot["_entry_attempts"] += 1
            bot["status"] = "CLOSING" if body["reduceOnly"] else "ENTRY_PENDING"
            emit(state, stamp, bot, 6 if body["reduceOnly"] else 3, "exchange_order_intent",
                 intent_message(body), request=body, order_body=order_body(body))
            self.save(state, recovery=recovery_write)
            # Linearization point: Stop after this durable send may race an in-flight IOC.
            # Any actual fill will be reconciled and reduced; no subsequent add can be sent.
        sent, rejected = self.clock(), None
        try:
            api.order(body)
            outcome = "acknowledged"
        except OrderRejected as error:  # sCode for this client ID and no ordId: the venue created no order
            outcome, rejected = "rejected", error.code
        except (PreflightError, OSError, ValueError):
            outcome = "uncertain"
            with self.lock:
                order["ack_uncertain"] = True
                emit(state, self.clock(), bot, 3 if not body["reduceOnly"] else 6, "order_ack_uncertain",
                     "Відповідь POST непевна: тільки GET за цим client ID, без повторної купівлі.")
                # The send is already durable. A Stop during I/O now owns the
                # recovery writer for factual evidence, never for another entry.
                self.save(state)
        with self.lock:
            order.update(post_sent_at=sent, post_answered_at=self.clock())
            if body["reduceOnly"] and outcome == "acknowledged":
                bot.pop("_close_refusals", None)
                bot.pop("_close_retry_at", None)
            emit(state, self.clock(), bot, 3 if not body["reduceOnly"] else 6, "exchange_order_posted",
                 "POST заявки: час надсилання й відповіді біржі.", client_order_id=body["clOrdId"], outcome=outcome,
                 post_seconds=round(order["post_answered_at"] - sent, 3), **({"rejected_code": rejected} if rejected else {}))
        if rejected:
            self.not_created(state, bot, api, order, rejected)
            return True
        # Even a successful acknowledgement is not a fill.
        result = self.order_facts(state, bot, api, order)
        if result is not None:  # an uncertain send the venue does not show yet: the account checks decide
            with self.lock:
                engine.record_result(state, bot, order, result, self.clock(), seen_by="submit")
                engine.mark(state, bot, last_snapshot(state, bot) if engine.mixed(state) else state.get("_last"))
                self.save(state)
        if self.resources[state["id"]].get("hosted_protection"):
            protection.ensure(self, state, bot, api)
            self.note_protection(state, bot)
        return True

    def identity_for(self, state, bot, kind):
        index = state["bots"].index(bot)
        return "J5" + state["id"][:16] + str(index) + kind + str(len(bot["_orders"])).zfill(4)

    def order_facts(self, state, bot, api, order):
        """protection.result_for, or None while an order whose send was uncertain is not visible on the venue (51603).
        Each such read is counted on the order; conclude_absent decides. R2.1.7 (R216-F3-R): an acknowledged order the
        venue no longer shows is counted the same way (OKX drops a cancelled unfilled order after about 2 hours, and its
        final facts may not have been read before a pause); a stop's child that is not found stays an error."""
        try:
            result = protection.result_for(api, bot, order)
        except OrderAbsent:
            if order.get("_protective_parent"):
                raise
            with self.lock:
                absent = order.setdefault("_absent", {"reads": 0, "first": self.clock()})
                absent.update(reads=absent["reads"] + 1, last=self.clock())
                if absent["reads"] == 1:  # one event and checkpoint; later reads (each second while it would rest) ride on the next save
                    emit(state, self.clock(), bot, 6 if order["request"]["reduceOnly"] else 3, "exchange_order_not_visible",
                         "OKX не показує заявку за цим client ID; перечитуємо, нову заявку не надсилаємо.",
                         client_order_id=order["request"]["clOrdId"])
                    self.save(state)
            return None
        if order.get("_absent"):
            with self.lock:  # seen again: a later absence counts its reads and time from scratch
                order.pop("_absent", None)
        return result

    def conclude_absent(self, state, bot, api, pending_ids):
        """R2.1.5: an uncertain send the venue answered "does not exist" ABSENT_READS times over ABSENT_SECONDS, not among
        the pending orders, with the position equal to own fills (the caller checked), was never created: terminal with
        no fill and an event, never an unresolved order that pauses the season. A close is sent again by closing().
        R2.1.7: first one cancel of its client ID (absent_cancel); only "nothing to cancel" keeps the verdict."""
        for order in engine.unresolved(bot):
            absent, known = order.get("_absent"), order.get("result")
            if ((known and D(known["contracts"])) or not absent or absent["reads"] < ABSENT_READS
                    or absent["last"] - absent["first"] < ABSENT_SECONDS or order["request"]["clOrdId"] in pending_ids):
                continue  # facts with a fill are never concluded away: their GET must answer
            resting = order["request"]["ordType"] == "post_only" and not order["request"]["reduceOnly"]
            if not absent.get("nothing_at"):
                if self.absent_cancel(state, bot, api, order) != "nothing":
                    continue  # it exists (the cancel was accepted) or the answer is unknown: it stays unresolved
                if api.position() != D(bot["_contracts"]):
                    continue  # R2.1.8 (R217-A1): 51400 also means "filled": a fill since the check's position read is no absence
                if resting:
                    # R2.1.9 (R218-A1A3-LAG): a resting entry gets its verdict only at the next check, if the position still
                    # equals own fills then: OKX's position can lag the fill its 51400 spoke of by more than one read, and
                    # until the verdict the order stays unresolved (no second entry; a late fill is late facts, not foreign)
                    with self.lock:
                        absent["nothing_at"] = self.clock()
                        self.save(state)
                    continue
            close = order["request"]["reduceOnly"]
            # "absent": a send that may never have reached the venue; "unseen": an order that existed (acknowledged, read,
            # or found by our cancel) and that the venue no longer shows, with no fill in the position
            kind = "absent" if uncertain_send(order) and not known and not absent.get("found_by_cancel") else "unseen"
            with self.lock:
                engine.record_not_created(state, bot, order, self.clock(), kind=kind)
                if not close and kind == "absent":  # as after a refused entry: no new entry before the recipe's cooldown
                    recipe = next(r for r in state["strategies"] if r["id"] == bot["id"])
                    bot["_next_decision"] = max(bot.get("_next_decision", 0), self.clock() + recipe["rules"]["cooldown_seconds"])
                if kind == "absent":
                    message = ("Заявку закриття біржа так і не показала: її не створено. Закриття надішлемо знову." if close else
                               "Заявку входу біржа так і не показала: її не створено, заповнень немає; спробу витрачено.")
                else:
                    message = ("Заявки, яка існувала, OKX більше не показує (скасовану без заповнення біржа зберігає ≈ 2 год), "
                               "а позиція заповнень від неї не має: записуємо її завершеною без заповнення."
                               + (" Закриття надішлемо знову." if close else ""))
                emit(state, self.clock(), bot, 6 if close else 3, "exchange_order_absent", message,
                     client_order_id=order["request"]["clOrdId"], absent_reads=absent["reads"],
                     absent_seconds=round(absent["last"] - absent["first"], 3), kind=kind)
                if not close and kind == "unseen":
                    # R2.1.11 (R219-UNSEEN-HOLD): the position may still lag a fill of it: no entry or add until an account
                    # check that started after this verdict has finished (this verdict came from the step's own check)
                    bot["_unseen_hold"] = self.clock()
                    emit(state, self.clock(), bot, 3, "entry_hold", "Нових входів і докупівель немає до наступної повної перевірки рахунку.",
                         reason="unseen_hold", client_order_id=order["request"]["clOrdId"])
                self.save(state)

    def not_created(self, state, bot, api, order, code):
        """R2.1.5: the venue refused this POST (a 51xxx sCode, no ordId): terminal with no fill, never the unresolved
        order that used to pause the season. An entry keeps its spent attempt and waits the recipe's cooldown; a close
        re-reads the owned stop, whose child is the close when it fired first, and the next account check compares the
        position with own fills."""
        close = order["request"]["reduceOnly"]
        recipe = next(r for r in state["strategies"] if r["id"] == bot["id"])
        with self.lock:
            engine.record_not_created(state, bot, order, self.clock(), kind="rejected", code=code)
            if not close:
                bot["_next_decision"] = max(bot.get("_next_decision", 0), self.clock() + recipe["rules"]["cooldown_seconds"])
            else:  # R2.1.7: a short flat pause (the refused close spends no limit); none without an armed stop
                bot["_close_refusals"] = bot.get("_close_refusals", 0) + 1
                bot["_close_retry_at"] = self.clock() + engine.CLOSE_RETRY_SECONDS
            emit(state, self.clock(), bot, 6 if close else 3, "exchange_order_rejected",
                 ("OKX відхилив закриття: ордер не створено. Перечитуємо власний stop і позицію та приймаємо фактичний стан; "
                  f"наступне закриття — не раніше ніж за {engine.CLOSE_RETRY_SECONDS} с (одразу, якщо stop на біржі не стоїть)." if close else
                  "OKX відхилив заявку входу: ордер не створено, спробу витрачено; наступний вхід — не раніше cooldown рецепта."),
                 client_order_id=order["request"]["clOrdId"], rejected_code=code,
                 **({"retry_after_seconds": engine.CLOSE_RETRY_SECONDS, "refusals": bot["_close_refusals"]} if close else {}))
            self.save(state)
        if close and self.resources[state["id"]].get("hosted_protection"):
            protection.reconcile(self, state, bot, api)

    def cancel_answer(self, state, bot, api, order):
        """One cancel POST of an own client ID (journalled, within cancel_tries): "accepted" (sCode 0: a live order was
        there), "nothing" (NOTHING_TO_CANCEL_CODES: filled, cancelled or never created), "unknown" otherwise."""
        with self.lock:
            self.count_cancel(state, order)
            self.save(state)
        sent, answer, code = self.clock(), "unknown", None
        try:
            api.cancel(order["request"]["clOrdId"])
            answer = "accepted"
        except CancelRefused as error:
            answer, code = "nothing", error.code
        except CancelFailed as error:  # R2.1.8 (R217-A2): the venue's own code, for the owner's Demo check
            code = error.code
        except (PreflightError, OSError, ValueError):
            pass
        with self.lock:
            emit(state, self.clock(), bot, 6 if order["request"]["reduceOnly"] else 3, "exchange_cancel_posted",
                 "POST скасування заявки: час надсилання й відповіді біржі.", client_order_id=order["request"]["clOrdId"],
                 outcome={"accepted": "acknowledged", "nothing": "nothing_to_cancel", "unknown": "uncertain"}[answer],
                 post_seconds=round(self.clock() - sent, 3), **({"cancel_code": code} if code else {}))
            self.save(state)
        return answer

    def absent_cancel(self, state, bot, api, order):
        """R2.1.7 (R216-F2-R1): before the absent verdict, one cancel of the client ID, as cancel_tries allows. An accepted
        cancel proves a live order the venue hides: it stays unresolved and its reads start again (no new entry beside it,
        no DONE while it rests). "Nothing to cancel" keeps the verdict; any other answer waits for the next check."""
        if not self.cancel_due(state, order):
            return "unknown"
        answer = self.cancel_answer(state, bot, api, order)
        if answer == "accepted":
            with self.lock:
                order["_absent"] = {"reads": 0, "first": self.clock(), "found_by_cancel": True}
                emit(state, self.clock(), bot, 6 if order["request"]["reduceOnly"] else 3, "exchange_order_found",
                     "Скасування прийнято: заявка існує, хоча OKX її не показує. Ведемо її далі, нових входів поруч немає.",
                     client_order_id=order["request"]["clOrdId"])
                self.save(state)
        return answer

    def unwatched_entries(self, state, bot, api):
        """R2.1.7 (R216-F2-R1): before a bot settles, one cancel of each post-only entry recorded as not created in the
        last NOT_CREATED_RECHECK_SECONDS: an accepted one existed after all and is reopened (True: the settlement waits for
        its facts); "nothing to cancel" is recorded once; an unknown answer is a fault, so the settlement tries again."""
        for order in bot["_orders"]:
            gone = order.get("not_created")
            if (not gone or gone.get("cancel_checked") or order["request"]["ordType"] != "post_only" or order["request"]["reduceOnly"]
                    or self.clock() - gone["at"] > NOT_CREATED_RECHECK_SECONDS):
                continue
            answer = self.cancel_answer(state, bot, api, order)
            if answer == "accepted":
                self.reopen(state, bot, order, "cancel")
                return True
            if answer != "nothing":
                raise PreflightError("not_created_entry_cancel_unconfirmed")
            with self.lock:
                gone["cancel_checked"] = True
                self.save(state)
        return False

    def reopen(self, state, bot, order, evidence):
        """R2.1.6: an order recorded as not created that the venue shows after all (its pending row, or facts in its
        GET): it becomes an ordinary unresolved order again, so its facts are read, a fill gets the owned stop, and a
        resting entry is cancelled when due."""
        with self.lock:
            was = order.pop("not_created")
            order["result"] = None
            order.pop("_absent", None)
            bot["status"] = "CLOSING" if bot["stop_requested"] or bot.get("_exit_requested") else "ENTRY_PENDING"
            emit(state, self.clock(), bot, 6 if order["request"]["reduceOnly"] else 3, "exchange_order_reopened",
                 "Біржа показала заявку, яку записали як не створену: ведемо її далі як звичайну.",
                 client_order_id=order["request"]["clOrdId"], reopened_from=was["kind"], evidence=evidence)
            self.save(state)

    def adopt_hidden(self, state, bot, api):
        """R2.1.6: the position differs from own fills; an order recorded as not created in the last
        NOT_CREATED_RECHECK_SECONDS may exist after all (a refusal answer for an order the venue did create): its GET
        decides. True when one was reopened with its facts."""
        found = False
        for order in bot["_orders"]:
            if not order.get("not_created") or self.clock() - order["not_created"]["at"] > NOT_CREATED_RECHECK_SECONDS:
                continue
            try:
                result = protection.result_for(api, bot, order)
            except OrderAbsent:
                continue
            self.reopen(state, bot, order, "order_get")
            with self.lock:
                engine.record_result(state, bot, order, result, self.clock(), seen_by="account_check")
                self.save(state)
            found = True
        return found

    def position_facts(self, state, bot, api, position):
        """R2.1.15 (Judge's conditions 3-4; plan check pp. 1, 2, 6; D181 p. 2): a leveraged bot's position on a read that
        agrees with its own fills. Each new size: entry_position_facts (leverage, margin mode, notional in USDT from the
        contracts and avgPx, OKX's notionalUsd apart, the liquidation price, its source and distance). Without OKX's liqPx the
        engine computes the price (liquidation_source "computed") and reads OKX's on the next two checks; when it comes, the
        facts again with "okx". Only with neither known after three reads: liquidation_unknown, which blocks the bot's entries
        and adds. A distance under twice the recipe's stop: liquidation_too_close, the same block for the rest of the run. A
        distance at or under the stop on any read closes the position reduce-only."""
        with self.lock:
            if not position:
                bot.pop("_facts_contracts", None)
                bot.pop("_liq_reads", None)
                bot.pop("_facts_source", None)
                return
            facts = api.last_position or {}
            recipe = next(r for r in state["strategies"] if r["id"] == bot["id"])
            stop = D(str(recipe["rules"]["stop_loss_bps"]))
            avg, okx, margin = venue_price(facts.get("avgPx")), venue_price(facts.get("liqPx")), venue_price(facts.get("margin"))
            computed, tier = (None, None) if okx else computed_liquidation(bot, position, avg, margin)
            liq = okx if okx is not None else computed  # a computed 0: a 1x long, never liquidated
            distance = (abs(avg - liq) / avg * 10000).quantize(D("0.01")) if avg and liq is not None else None
            known = distance is not None
            size, changed = str(position), False
            fresh = bot.get("_facts_contracts") != size
            if fresh:
                bot.update(_facts_contracts=size, _liq_reads=0)
            reads = bot.get("_liq_reads")
            source = ("okx" if okx is not None else "model_1x_long" if computed == 0 else "computed") if known else None
            if reads is not None:
                changed = True
                if fresh or FACT_RANK[source] > FACT_RANK[bot.get("_facts_source")]:  # RC2 (LL-04): a better price only
                    bot["_facts_source"] = source
                    usd = venue_price(facts.get("notionalUsd"))
                    emit(state, self.clock(), bot, 3, "entry_position_facts",
                         "Факти позиції з плечем: нотіонал, ціна ліквідації OKX і відстань до неї." if okx else
                         "Факти позиції з плечем; OKX не дав ціни ліквідації — рахуємо її за тиром і перечитаємо." if known else
                         "Факти позиції з плечем; ціни ліквідації поки немає — перечитаємо на наступній перевірці.",
                         leverage=int(engine.leverage(bot)), margin_mode=facts.get("mgnMode"), position_contracts=size,
                         avg_price=engine.N(avg) if avg else None,
                         notional_usdt=engine.N(abs(position) * engine.contract_btc(bot) * avg) if avg else None,
                         notional_usd=engine.N(usd) if usd else None, margin_usdt=engine.N(margin) if margin else None,
                         liquidation_price=engine.N(liq) if known else None,
                         liquidation_source=source,
                         **({"tier_mmr": tier["mmr"]} if tier and not okx else {}),
                         liquidation_distance_bps=engine.N(distance) if known else None,
                         required_distance_bps=engine.N(2 * stop), stop_loss_bps=engine.N(stop),
                         distance_ok=(distance >= 2 * stop) if known else None, liquidation_reads=reads + 1)
                    if known and distance < 2 * stop and not bot.get("entries_blocked"):
                        bot["entries_blocked"] = "liquidation_too_close"
                        emit(state, self.clock(), bot, 3, "liquidation_too_close",
                             "Ліквідація ближча за подвійний стоп рецепта: нові входи й докупівлі цього бота заборонено до кінця раунду.",
                             liquidation_distance_bps=engine.N(distance), required_distance_bps=engine.N(2 * stop))
                if source == "okx":
                    bot["_liq_reads"] = None
                elif reads + 1 < 3:
                    bot["_liq_reads"] = reads + 1
                else:
                    bot["_liq_reads"] = None
                    if not known and not bot.get("entries_blocked"):
                        bot["entries_blocked"] = "liquidation_unknown"
                        emit(state, self.clock(), bot, 3, "liquidation_unknown",
                             "Ціни ліквідації немає ні від OKX, ні з розрахунку: нові входи й докупівлі цього бота заборонено до кінця раунду.",
                             position_contracts=size, liquidation_reads=3)
            if known and distance <= stop and not bot["stop_requested"]:
                changed = True
                emit(state, self.clock(), bot, 5, "liquidation_closer_than_stop",
                     "Ліквідація не далі за стоп рецепта: стоп уже не захищає, закриваємо позицію reduce-only.",
                     liquidation_distance_bps=engine.N(distance), stop_loss_bps=engine.N(stop),
                     liquidation_source=source)
                engine.stop(state, self.clock(), bot["id"], "Ліквідація ближча за стоп рецепта.", code="risk_limit")
            if changed:
                self.save(state)

    def liquidation_seen(self, state, bot, api):
        """R2.1.15 (plan check p. 8): before a leveraged bot's position mismatch goes to the manual review, its bills since
        the bot began: OKX's liquidation and ADL rows of its contract -> liquidation_detected. The manual review follows as
        before (MANUAL_REVIEW_REASONS and incomplete unchanged); a failed read of the bills changes nothing."""
        try:
            rows = api.bills(bot["_begin_ms"])
        except (PreflightError, OSError, ValueError, KeyError, TypeError):
            return
        named = set(bot.get("_liquidation_bills") or ())  # RC2 (LL-01): named once, whatever the Resumes
        found = [r for r in rows if r.get("instId") == api.inst_id and r.get("billId") not in named
                 and str(r.get("subType")) in LIQUIDATION_SUBTYPES | ADL_SUBTYPES]
        if not found:
            return
        kinds = {"adl" if str(r["subType"]) in ADL_SUBTYPES else "liquidation" for r in found}
        times = [int(r["ts"]) for r in found if isinstance(r.get("ts"), str) and r["ts"].isdigit()]
        with self.lock:
            bot["_liquidation_bills"] = sorted(named | {r["billId"] for r in found})
            emit(state, self.clock(), bot, 6, "liquidation_detected",
                 "OKX примусово закрив позицію (ліквідація чи ADL): далі ручна звірка.",
                 kind=kinds.pop() if len(kinds) == 1 else "liquidation_and_adl", bill_rows=len(found),
                 bill_subtypes=sorted({str(r["subType"]) for r in found}), contracts=venue_sum(found, "sz"),
                 pnl_usdt=venue_sum(found, "pnl", signed=True),
                 fee_usdt=venue_sum(found, "fee"), venue_at_ms=max(times) if times else None)
            self.save(state)

    def settled_check(self, state, resource, bot, api):
        """R2.1.14 (R2113-RESUME-SETTLED, D163 p. 2): the one account read of a bot that settled before the pause. Flat and
        no order on its subaccount: its accounting stands, no second settlement (settled_account_verified). A foreign row:
        the manual review, its accounting of our own trades kept. A transient code: the usual failure path (A1 facts, A2
        codes), read again next step; 120 s after the Resume with no read, the accounting is left unconfirmed as before."""
        try:
            if api.position():
                raise PreflightError("actual_position_differs_from_owned_fills")
            api.pending()  # any row on a settled subaccount is foreign: foreign_or_unbounded_pending_orders, foreign_algo_order
        except (PreflightError, ArenaError, OSError, ValueError, KeyError, TypeError) as error:
            with self.lock:
                self.account_failure(state, resource, bot, error, None)
                if bot["_manual_review"]:
                    bot.pop("_settled_check")
                elif self.clock() - bot["_closing_since"] >= 120:
                    bot.pop("_settled_check")
                    bot["accounting_reconciled"] = False
                    self.settled_window_end(state, bot, "stopped")
                    emit(state, self.clock(), bot, 6, "settled_account_unverified",
                         "Рахунок звіреного до паузи бота не прочитано за 120 с: звірку не підтверджено, як до R2.1.14.")
                self.save(state)
            return
        with self.lock:
            bot.pop("_settled_check")
            self.settled_window_end(state, bot, "recovered")
            emit(state, self.clock(), bot, 6, "settled_account_verified",
                 "Бот звірив облік до паузи; після Resume рахунок порожній, без заявок: звірку збережено.")
            self.save(state)

    def settled_window_end(self, state, bot, outcome):
        """R2.1.14 RC2 (RC-04): the end of a settled bot's one read after a Resume ends its degraded window, its failure
        count and its account block, so an outage is one window and a STOPPED bot keeps no block."""
        if bot.get("_degraded"):
            self.end_window(state, bot, outcome)
        bot["_api_failures"] = 0
        if bot.get("admission_blocked") in {"account_unavailable", "degraded_window"}:
            bot.pop("admission_blocked")

    def stop_fired_late(self, state, bot, api):
        """R2.1.14 (D164; plan check p. 6): the second window of the same race. Our own stop fired after this check's position
        GET, and ensure saw its child (no recovery for that any more, protection.own_stop_fired). The child is read as in the
        first window (reconcile), then the position again. The facts of the fire with a position that agrees are
        protective_fired_during_check and the cleanup of the flat protection; a child's fill or a position view OKX does not
        show yet is the same bounded wait (stop_fire_pending); anything else is the manual review, as before."""
        record = next((p for p in bot.get("_protections", []) if not p.get("terminal")), None)
        if record is None:
            raise PreflightError("actual_position_differs_from_owned_fills")  # RC2 (RC-05): never a StopIteration past the step
        protection.reconcile(self, state, bot, api)
        position = api.position()
        if position == D(bot["_contracts"]) and self.stop_fire_proof(bot, record):
            self.stop_fire_seen(state, bot, record)
        elif position == D(bot["_contracts"]) and protection.fired_child_unread(bot, record):
            self.stop_fire_wait(state, bot, record, position)  # fired, its child's fill not read yet: stop_fire_pending
            raise PreflightError("actual_position_differs_from_owned_fills")  # the wait ended at its limit
        elif self.stop_fired(state, bot, api, position) != D(bot["_contracts"]):
            raise PreflightError("actual_position_differs_from_owned_fills")
        protection.ensure(self, state, bot, api)  # flat now: the cleanup marks the protection FLAT

    def stop_fire_proof(self, bot, record):
        """R2.1.14 RC2 (RC-01, D169 p. 1): the facts of the fire itself, our record read effective or partially_effective
        (GET order-algo) and its adopted children filled exactly the size it protected. A position that agrees with own
        fills proves nothing alone (a by-hand close and reopen, a read glitch)."""
        filled = sum((D((o.get("result") or {}).get("contracts") or "0") for o in bot["_orders"]
                      if o.get("_protective_parent") == record["body"]["algoClOrdId"]), D("0"))
        return ((record.get("proof") or {}).get("state") in {"effective", "partially_effective"}
                and filled == D(record["body"]["sz"]))

    def fire_record(self, state, bot, position):
        """R2.1.14: the record of our own stop that explains a venue position other than own fills, or None. No order of ours
        but the stop's children may be open; an open wait keeps its own record (algo_client_id).
        - The venue ahead: the venue flat, the record open and live for the whole position (stop_matches, as A3 takes it) or
          shown fired with a child not read or filled yet (fired_child_unread).
        - The venue behind (RC2, RC-02): the record fired, its child adopted in the last STOP_FIRE_WAIT_SECONDS with a fill,
          and the venue shows exactly the size before that fill (OKX's position view a moment late, R218-A1A3-LAG)."""
        if any(not o.get("_protective_parent") for o in engine.unresolved(bot)):
            return None
        wait, records = bot.get("_stop_fire_wait"), bot.get("_protections") or []
        if wait is not None:
            chosen = [p for p in records if p["body"]["algoClOrdId"] == wait["algo_client_id"]]
        else:
            chosen = [p for p in records if not p.get("terminal")] or records[-1:]
        if len(chosen) != 1 or chosen[0].get("stop_fire_spent"):
            return None  # D169 p. 2: a record whose wait reached its limit explains nothing more
        record = chosen[0]
        if position == 0 and (protection.stop_matches(state, bot, record) or protection.fired_child_unread(bot, record)):
            return record  # neither holds for a terminal record
        children = [o for o in bot["_orders"] if o.get("_protective_parent") == record["body"]["algoClOrdId"] and o.get("result")]
        if not self.stop_fire_proof(bot, record) or any(self.clock() - o["at"] > STOP_FIRE_WAIT_SECONDS for o in children):
            return None
        before = D(bot["_contracts"]) + sum((D(o["result"]["contracts"]) if o["request"]["side"] == "sell" else -D(o["result"]["contracts"]))
                                            for o in children)
        return record if position == before else None

    def stop_fired(self, state, bot, api, position):
        """R2.1.14 (R2113-STOP-FIRED-LAG, D160, D161; plan check pp. 2-10; RC2). A venue position other than own fills that our
        own stop record explains (fire_record): the stop is read again first, then the position. The facts of the fire with
        a position that agrees end it: protective_fired_during_check, or protective_fire_resolved for an open wait. A
        position that agrees while our stop did not fire (a read glitch, a by-hand reopen) is the manual review, as before
        (RC-01). Facts the venue does not show yet keep the bot waiting (stop_fire_wait). Anything else, or the end of the
        wait: the manual review as before. Returns the position the caller compares with own fills."""
        record = self.fire_record(state, bot, position)
        if record is None:
            self.end_stop_fire_wait(state, bot, "other_facts")
            return position
        try:
            protection.reconcile(self, state, bot, api)  # first the stop, then the limit (plan check p. 2)
        except PreflightError as error:
            if not unseen_receipt(error):
                raise
            self.stop_fire_wait(state, bot, record, position)  # QA-03: the facts not visible yet, the same bounded wait
            return position  # the wait ended at its limit with no proof: the manual review
        again = api.position()
        if again == D(bot["_contracts"]):
            if self.stop_fire_proof(bot, record):
                self.stop_fire_seen(state, bot, record)
                return again
            if protection.fired_child_unread(bot, record):
                self.stop_fire_wait(state, bot, record, again)  # fired, its child still filling: stop_fire_pending
                return position  # the wait ended at its limit with no proof: the manual review
            self.end_stop_fire_wait(state, bot, "stop_not_fired")
            return position  # RC-01: the first read differed and our stop did not fire: the manual review
        if self.fire_record(state, bot, again) is not record:
            self.end_stop_fire_wait(state, bot, "other_facts")
            return again  # canceled or failed (reconcile asked for the recovery), or another size: as before
        self.stop_fire_wait(state, bot, record, again)
        return again  # the wait ended at its limit

    def stop_receipt_unseen(self, state, bot, error):
        """R2.1.15 (D183 p. 3, QA-03): with a wait for our own stop's facts open, OKX's 51603 on the GET of that stop is the
        facts not visible yet (seen 1-3 s after OKX took a stop off: season 6d794711), one more check of the same bounded
        wait (stop_fire_pending), never a foreign state; at the wait's limit the manual review, as before. Returns quietly
        otherwise, and the caller raises the error as it is."""
        wait = bot.get("_stop_fire_wait")
        if wait is None or not unseen_receipt(error):
            return
        record = next((p for p in bot.get("_protections", []) if p["body"]["algoClOrdId"] == wait["algo_client_id"]), None)
        if record is None or record.get("receipt") is None:
            return  # QR-01, not QA-03: a stop OKX never showed has its own verdict (protection._never_shown)
        self.stop_fire_wait(state, bot, record, D(bot["_contracts"]))
        raise PreflightError("actual_position_differs_from_owned_fills")  # the wait ended at its limit with no proof

    def stop_fire_wait(self, state, bot, record, position):
        """R2.1.14: one more check of the bounded wait for the facts of our own stop's fire (record), at most
        STOP_FIRE_WAIT_CHECKS checks or STOP_FIRE_WAIT_SECONDS from the first one by clock() (a Resume starts the monotonic
        clock anew), whichever comes first. Raises stop_fire_pending, which the step and the entry recheck take for no
        account failure (no A3, no degraded window, no failure count): no entry, exit, close or new stop meanwhile. At its
        end with the fire proven (D169 p. 2) only this bot stops: the recovery, as R2.1.13 for this fire, and
        stop_fire_recovery, which the step takes like stop_fire_pending; with no proof it returns, and the caller's manual
        review follows."""
        now = self.clock()
        with self.lock:
            wait = bot.get("_stop_fire_wait")
            if wait is None:
                wait = bot["_stop_fire_wait"] = {"first_at": now, "checks": 0, "algo_client_id": record["body"]["algoClOrdId"]}
                emit(state, now, bot, 6, "protective_fire_pending",
                     "Позиція на біржі й факти власного stop ще не збігаються: чекаємо їх, без входу й виходу.",
                     contracts=record["body"]["sz"], venue_state=(record.get("proof") or {}).get("state"),
                     stop_fire_venue_contracts=str(position), stop_fire_own_contracts=bot["_contracts"])
            wait["checks"] += 1
            if wait["checks"] >= STOP_FIRE_WAIT_CHECKS or now - wait["first_at"] >= STOP_FIRE_WAIT_SECONDS:
                proven = self.stop_fire_proof(bot, record)
                self.end_stop_fire_wait(state, bot, "wait_limit")
                if proven:  # D169 p. 2: the fire is proven, OKX's position view still behind: only this bot stops
                    record["stop_fire_spent"] = True
                    protection.request_recovery(self, state, bot)
                    raise PreflightError("stop_fire_recovery")
                return
            bot["admission_blocked"] = "stop_fire_pending"
            self.save(state)
        raise PreflightError("stop_fire_pending")

    def end_stop_fire_wait(self, state, bot, outcome):
        """R2.1.14 (RC2, RC-05): a wait that ends without the facts of the fire (outcome wait_limit, stop_not_fired,
        other_facts or manual_review) leaves no key behind and journals its end; the manual review follows."""
        with self.lock:
            wait = bot.pop("_stop_fire_wait", None)
            if bot.get("admission_blocked") == "stop_fire_pending":
                bot.pop("admission_blocked")  # the manual review that follows names its own reason
            if wait is None:
                return
            now = self.clock()
            emit(state, now, bot, 6, "protective_fire_expired", "Факти власного stop не підтвердились у межах вікна: ручна звірка.",
                 outcome=outcome, stop_fire_wait_checks=wait["checks"], stop_fire_wait_seconds=round(now - wait["first_at"], 3))
            self.save(state)

    def stop_fire_end(self, state, bot):
        """R2.1.14 RC2 (RC-01): an open wait whose position agrees again ends resolved only with the facts of the fire of its
        own record; a position back while our stop did not fire (a by-hand close and reopen) is the manual review."""
        wait = bot["_stop_fire_wait"]
        record = next((p for p in bot.get("_protections", []) if p["body"]["algoClOrdId"] == wait["algo_client_id"]), None)
        if record is not None and self.stop_fire_proof(bot, record):
            self.stop_fire_seen(state, bot, record)
            return
        if record is not None and protection.fired_child_unread(bot, record):
            self.stop_fire_wait(state, bot, record, D(bot["_contracts"]))  # its child still filling: stop_fire_pending
        self.end_stop_fire_wait(state, bot, "stop_not_fired")
        raise PreflightError("actual_position_differs_from_owned_fills")

    def stop_fire_seen(self, state, bot, record):
        """R2.1.14: the facts of our own stop's fire are read and the position agrees: an open wait ends resolved (also after
        its limit, e.g. a Resume hours later), or a fire inside one check was caught by its second read."""
        now = self.clock()
        with self.lock:
            wait = bot.pop("_stop_fire_wait", None)
            if wait is not None:
                emit(state, now, bot, 6, "protective_fire_resolved", "Факти власного stop прочитано: позиція звірена.",
                     stop_fire_wait_checks=wait["checks"], stop_fire_wait_seconds=round(now - wait["first_at"], 3))
            else:
                receipt = record.get("receipt") or {}
                updated = str(receipt.get("triggerTime") or receipt.get("uTime", ""))  # OKX: the trigger's own time first
                emit(state, now, bot, 6, "protective_fired_during_check", "Власний stop спрацював під час перевірки рахунку: прочитано повторно.",
                     **({"seconds_from_venue_fire": round(now - int(updated) / 1000, 3)} if updated.isdigit() else {}))
            self.save(state)  # the step lifts the block itself after a check that passed

    def unseen_explains(self, state, bot, position):
        """R2.1.6: the position differs from own fills by no more than what sent orders the venue does not show yet
        (51603) could have filled: their facts are late, not foreign activity (R2.1.5 took it for a foreign position and
        paused). Tolerated for 2 x ABSENT_SECONDS from the first such read, entries stay blocked meanwhile (the orders are
        unresolved); then the difference is a manual review again. One event per order."""
        # R2.1.8 (R217-A3): no window for orders already concluded absent: the position read after their "nothing to
        # cancel" (R217-A1) covers the fill race, and for IOC orders and closes the window could only hide foreign activity
        # an order read as resting with no fill that the venue then stopped showing counts too (R2.1.8, R217-A1)
        unseen = [o for o in engine.unresolved(bot) if o.get("_absent") and not D((o.get("result") or {}).get("contracts") or "0")]
        if not unseen or self.clock() - min(o["_absent"]["first"] for o in unseen) > 2 * ABSENT_SECONDS:
            return False
        low, high = engine.unseen_range(unseen)
        if not low <= position - D(bot["_contracts"]) <= high:
            return False
        with self.lock:
            for order in unseen:
                if not order["_absent"].get("lag_noted"):
                    order["_absent"]["lag_noted"] = True
                    emit(state, self.clock(), bot, 6 if order["request"]["reduceOnly"] else 3, "exchange_order_fill_not_visible",
                         "Позиція вже змінилася, а OKX ще не показує заявку, яка могла це зробити: чекаємо її фактів, нових входів немає.",
                         client_order_id=order["request"]["clOrdId"], position_contracts=str(position), owned_contracts=bot["_contracts"])
            self.save(state)
        return True

    def check_exits(self, state, resource, bot, recipe, snapshot):
        """The step's exit rules on the bot's own position (under the lock): the stop, the target, the hold, the formal
        exit, the deadline and the risk limit. True when an exit fired now (a Stop or a close is requested)."""
        engine.mark(state, bot, snapshot)
        if snapshot:
            with instrument_block(state, bot):  # R2.1.12 (MIX): the V2 transition sees the contract's entry block
                runtime.advance(state, bot, recipe, snapshot, emit)
        reason = engine.exit_reason(state, bot, recipe, snapshot, self.clock())
        if bot["position_btc"] and recipe["rules"]["max_hold_seconds"] is not None:
            remaining = min(recipe["rules"]["max_hold_seconds"], max(0,
                recipe["rules"]["max_hold_seconds"] - (self.clock() - bot["_hold_start"])))
            hold_deadline = resource["hold_deadlines"].setdefault(bot["id"], self.monotonic() + remaining)
            if self.monotonic() >= hold_deadline:
                reason = reason or "Час утримання вичерпано (монотонний таймер)."
        else:
            resource["hold_deadlines"].pop(bot["id"], None)
            if state["status"] == "DRAINING" and not bot["position_btc"] and not engine.unresolved(bot) and not bot["stop_requested"]:
                engine.stop(state, self.clock(), bot["id"], "Вікно входів закрито; відкритої позиції немає.", code="window_closed_flat")
        if not reason or bot["stop_requested"]:
            return False
        stop_now = (is_probe(recipe) or (not draining_policy(state) and self.clock() >= state["_end"])
                    or bot["max_drawdown_pct"] >= 10)
        if not stop_now and bot.get("_exit_requested") and bot.get("_reduce_remaining") is None:
            return True  # R2110-DUP-EXIT: this full exit is already requested; its trigger was written when it was
        bot.pop("_reduce_remaining", None)
        why = ("risk_limit" if bot["max_drawdown_pct"] >= 10 else "max_hold" if "Час утримання" in reason else
               "stop_loss" if reason == "Локальний stop-loss стратегії." else "take_profit" if reason == "Ціль стратегії." else
               "rules_exit" if reason == "Формальна умова виходу." else "close_only" if is_probe(recipe) else
               "deadline" if reason == "Час сезону завершено." else "owner_stop")  # R2.1.10: deadline was owner_stop
        emit(state, self.clock(), bot, 5, "exit_trigger", "Зафіксовано умову виходу перед ордером.",
             exit_reason=why, **exit_facts(bot, recipe, snapshot, self.clock(), reason),
             exit_checks=(runtime.exit_view(recipe, bot, snapshot)[1] if runtime.enabled(recipe) else engine.evaluate(recipe["rules"]["exit"], snapshot["features"])[1]) if snapshot else [],
             snapshot_sha256=snapshot["sha256"] if snapshot else None,
             observed_at=self.clock(), bar_time_ms=snapshot.get("bar_close_ms") if snapshot else None,
             position_btc=bot["position_btc"], features=snapshot["features"] if snapshot else None,
             position_age_seconds=self.clock() - bot["_hold_start"] if bot.get("_hold_start") is not None else None)
        if stop_now:
            engine.stop(state, self.clock(), bot["id"], reason, code=why)
        else:
            bot.update(_exit_requested=True, _close_reason=reason, status="CLOSING",
                       _closing_since=bot.get("_closing_since") or self.clock())
            emit(state, self.clock(), bot, 5, "rules_close", reason + " Jev не затримує вихід.")
        return True

    def account_failure(self, state, resource, bot, error, snapshot, *, exits=True):
        """One failed account check of a bot (under the lock). A4: every run of them is a degraded window in the journal.
        A3 (BOARD 2def374): a position under an owned stop verified on the venue may run on for at most 120 s of transient
        OKX codes (exchange_diagnostics.TRANSIENT_CODES), its exits checked every step on fresh market data, no entry or
        add; True when such an exit asks for a close now. Otherwise, as before: 3 in a row -> recovery or PAUSED."""
        bot["_api_failures"] = bot.get("_api_failures", 0) + 1
        diagnostic = exchange_diagnostics.failure(error, stage="account_check", fallback="exchange_check_failed")
        message, facts = diagnostic["reason"], exchange_diagnostics.venue_facts(error)
        emit(state, self.clock(), bot, 4, "exchange_check_failed", diagnostic["message"],
             reason=message, stage=diagnostic["stage"], **facts)
        window = self.open_window(state, resource, bot, error, facts, snapshot)
        if message in exchange_diagnostics.MANUAL_REVIEW_REASONS:
            self.end_stop_fire_wait(state, bot, "manual_review")  # R2.1.14 RC2 (RC-05): no key of an open wait left behind
            self.end_extension(state, bot, window, "manual_review")
            bot["_manual_review"] = True
            state["incomplete"] = True
            engine.stop(state, self.clock(), reason="Стороння активність/неузгоджений акаунт: потрібна ручна перевірка.", code="manual_review")
            self.end_window(state, bot, "manual_review")
            return False
        if window.get("extension") == "active":
            why = self.extension_veto(state, resource, bot, window, error, snapshot)
            if why is None:
                if bot["_api_failures"] >= 3:
                    state["incomplete"] = True  # R2110-INCOMPLETE: as before, on the third failure; only the recovery waits
                bot["admission_blocked"] = "degraded_window"  # no entry, no add for the whole run
                if not exits:
                    return False  # R2110-DUP-EXIT: a failed close inside the window is one more failure, not one more exit
                recipe = next(r for r in state["strategies"] if r["id"] == bot["id"])
                self.check_exits(state, resource, bot, recipe, snapshot)
                return bool(bot["stop_requested"] or bot.get("_exit_requested"))
            self.end_extension(state, bot, window, why)
        if bot["_api_failures"] >= 3:
            state["incomplete"] = True
            if state.get("protocol_version", 0) >= 8:
                bot["admission_blocked"] = "account_unavailable"
                if D(bot["_contracts"]) and resource.get("hosted_protection"):
                    protection.request_recovery(self, state, bot)
                    window["outcome"] = "recovery"
                if engine.unresolved(bot):
                    state.update(status="PAUSED", error="Непевний ордер: потрібна звірка власником.")
                    window["outcome"] = "paused"
            else: engine.stop(state, self.clock(), reason="Три невдалі перевірки OKX: завершення з відновленням.", code="account_unavailable")
        return False

    def open_window(self, state, resource, bot, error, facts, snapshot):
        """A4: the bot's degraded window, opened by the first failure of a run; A3 decides its extension there."""
        now, mono, window = self.clock(), self.monotonic(), bot.get("_degraded")
        if window is None:
            entry = exchange_diagnostics.transient(error)
            matched = resource.setdefault("stop_matched_mono", {}).get(bot["id"])  # monotonic: a clock jump changes nothing
            own = [o for o in engine.unresolved(bot) if not o.get("_protective_parent")]
            live = [p for p in bot.get("_protections", []) if not p.get("terminal")]
            current = len(live) == 1 and protection.stop_matches(state, bot, live[0])  # still the actual size after an add or reduce
            eligible = (state.get("protocol_version", 0) >= 8 and bool(resource.get("hosted_protection")) and bool(D(bot["_contracts"]))
                        and entry is not None and entry["extension"] and matched is not None and 0 <= mono - matched <= EXTENSION_STOP_AGE_SECONDS
                        and current and not own and fresh_market(snapshot, now) and not bot["stop_requested"] and not bot.get("_exit_requested")
                        and not bot.get("_protection_recovery") and not bot.get("_stop_fire_wait"))  # R2.1.14 RC2 (RC-03)
            resource.setdefault("degraded_mono", {})[bot["id"]] = mono
            window = bot["_degraded"] = {"start": now, "failures": 0, "codes": [], "endpoints": [], "http_statuses": [],
                                         "skipped_decisions": 0, "extension": "active" if eligible else "none"}
            emit(state, now, bot, 4, "degraded_window_started",
                 "Перевірки OKX не проходять: нових входів немає" + ("; позиція під перевіреним стопом на біржі, виходи перевіряються кожен крок (до 120 с)."
                                                                       if eligible else "."),
                 extended=eligible, protected_age_seconds=round(mono - matched, 3) if matched is not None else None, **facts)
        window["failures"] += 1
        reason = exchange_diagnostics.failure(error, fallback="exchange_check_failed")["reason"]  # as its exchange_check_failed
        for key, value in (("codes", facts.get("okx_code") or reason),
                           ("endpoints", facts.get("endpoint")), ("http_statuses", facts.get("http_status"))):
            if value is not None and value not in window[key] and len(window[key]) < 10:
                window[key].append(value)
        recipe = next(r for r in state["strategies"] if r["id"] == bot["id"])
        if snapshot and not engine.unresolved(bot):  # the decisions this run skips: new bars (schema 4), else 30 s slots
            if v4rt.enabled(recipe) and not bot["position_btc"]:
                if v4rt.decision_due(bot, snapshot) and snapshot.get("bar_close_ms") != window.get("_bar"):
                    window.update(_bar=snapshot.get("bar_close_ms"), skipped_decisions=window["skipped_decisions"] + 1)
            elif now >= bot["_next_decision"]:
                slot = int((now - bot["_next_decision"]) // 30)
                if slot != window.get("_slot"):
                    window.update(_slot=slot, skipped_decisions=window["skipped_decisions"] + 1)
        return window

    def extension_veto(self, state, resource, bot, window, error, snapshot):
        """Why the A3 extension cannot go on at this failure, or None."""
        entry, now = exchange_diagnostics.transient(error), self.clock()
        if entry is None or not entry["extension"]:
            return "not_transient"
        started = resource.get("degraded_mono", {}).get(bot["id"])  # none after a restart: the process that opened it is gone
        if started is None or self.monotonic() - started >= EXTENSION_SECONDS:
            return "limit_120s"
        if bot["stop_requested"]:
            return "stopped"  # an owner Stop, a Resume or a system stop: the rule as before
        if not D(bot["_contracts"]):
            return "flat"  # R2110-INCOMPLETE: closed inside the window: nothing left to keep open
        live = [p for p in bot.get("_protections", []) if not p.get("terminal")]
        fired = len(live) == 1 and protection.fired_child_unread(bot, live[0])
        if not protection.closing_in_flight(bot) and not fired and not (len(live) == 1 and protection.stop_matches(state, bot, live[0])):
            # R2.1.11 (R2110-RECHECK): every step, the stop record as it stands must still match (the actual size after an add
            # or reduce, no amendment pending, live); never while our own reduce-only close is on its way, since OKX takes
            # the stop off at that close (cxlOnClosePos), the R219-CLOSE-LATE class, nor while the child of a stop that fired
            # is unread (R2111-RECHECK-FIRED). It only shortens the 120 s.
            return "stop_unmatched"
        if not fresh_market(snapshot, now):
            return "market_not_fresh"
        if any(not o.get("_protective_parent") and not o["request"]["reduceOnly"] for o in engine.unresolved(bot)):
            return "unresolved_order"
        if bot.get("_protection_recovery"):
            return "recovery"
        return None

    def end_extension(self, state, bot, window, why):
        if window.get("extension") == "active":
            window["extension"] = why
            emit(state, self.clock(), bot, 4, "degraded_extension_ended", "Продовження під стопом на біржі завершено; далі звичайні правила.",
                 reason=why, seconds=round(self.clock() - window["start"], 3), failures=window["failures"])

    def end_window(self, state, bot, outcome):
        """A4: the end of a degraded window, with its codes, endpoints, skipped decisions and outcome."""
        window = bot.pop("_degraded", None)
        (self.resources.get(state["id"]) or {}).get("degraded_mono", {}).pop(bot["id"], None)
        if bot.get("admission_blocked") == "degraded_window":
            bot.pop("admission_blocked")  # R2110-WINDOW-END: the window's admission block goes with it
        if window is None:
            return
        if window.get("extension") == "active":
            window["extension"] = outcome
        record = {"outcome": window.get("outcome", outcome), "start_ms": int(window["start"] * 1000),  # recovery or paused stays
                  "seconds": round(self.clock() - window["start"], 3), "failures": window["failures"], "codes": window["codes"],
                  "endpoints": window["endpoints"], "http_statuses": window["http_statuses"],
                  "skipped_decisions": window["skipped_decisions"], "extended": window["extension"] != "none",
                  "extension_end": window["extension"] if window["extension"] != "none" else None}
        emit(state, self.clock(), bot, 4, "degraded_window_ended", "Перевірки OKX знову проходять." if outcome == "recovered"
             else "Вікно непройдених перевірок OKX завершено.", **record)
        windows = state.get("degraded_windows")
        if isinstance(windows, list) and len(windows) < DEGRADED_WINDOWS_KEPT:  # R2.1.11 (A4): only a list a fresh Start made
            windows.append({"bot_id": bot["id"], **deepcopy(record)})

    def closing(self, state, bot, api):
        self.execution_owner_guard()
        if bot["_manual_review"] or engine.unresolved(bot):
            return
        api.recovery_transport = True
        if bot.get("_reduce_remaining") is not None and not bot["stop_requested"] and D(bot["_reduce_remaining"]) > 0 and engine.intent_count(bot) >= engine.LIMITS["max_order_intents_per_bot"] - 20:
            with self.lock:  # an owner Stop saves the same journal under this lock
                emit(state, self.clock(), bot, 5, "reduction_remainder_veto", "Ненадісланий залишок часткового скорочення скасовано; власна позиція лишається відкритою.",
                     reason="close_only", remaining_contracts=bot["_reduce_remaining"])
                bot.pop("_reduce_remaining", None)
                bot.update(_exit_requested=False, _close_reason=None, _closing_since=None, status="HOLDING", _next_decision=self.clock()+30)
                api.recovery_transport = False
                self.save(state)
            return
        if bot.get("_reduce_remaining") is not None and not bot["stop_requested"] and D(bot["_reduce_remaining"]) == 0 and D(bot["_contracts"]):
            with self.lock:
                bot.pop("_reduce_remaining", None)
                bot.update(_exit_requested=False, _close_reason=None, _closing_since=None, status="HOLDING", _next_decision=self.clock() + 30)
                api.recovery_transport = False
                self.save(state)
            return
        if (D(bot["_contracts"]) and self.resources[state["id"]].get("hosted_protection")
                and any(not p.get("terminal") for p in bot.get("_protections", []))):
            # R2.1.5: the owned stop may have fired since the account check (a local stop-loss on the same bar): its
            # child is then the close, and a reduce-only order for the old quantity would meet a flat position.
            protection.reconcile(self, state, bot, api)
        if D(bot["_contracts"]):
            if self.clock() < bot.get("_close_retry_at", 0) and any(
                    not p.get("terminal") and (p.get("proof") or {}).get("live_verified") for p in bot.get("_protections", [])):
                # R2.1.7: the last close ended with nothing; a short pause only while our stop stands live on the venue (its
                # last receipt; the status reads RECOVERY_CLOSE_ONLY while closing), never once it failed or was cancelled
                return
            maximum = bot.get("_reduce_remaining") if not bot["stop_requested"] else None
            self.submit(state, bot, api, engine.close_body(bot, self.identity_for(state, bot, "C"), maximum), self.clock())
            return
        if (self.resources[state["id"]].get("hosted_protection")
                and bot.get("protection", {}).get("status") != "FLAT"):
            return  # A cancel ACK is not cleanup proof; do not finish or re-enter.
        with self.lock:
            if not bot["stop_requested"] and state["status"] == "DRAINING":
                engine.stop(state, self.clock(), bot["id"], "Цикл завершено після дедлайну; повторного входу немає.", code="deadline")
            if not bot["stop_requested"]:
                api.recovery_transport = False
                bot.update(status="WAITING", _exit_requested=False, _close_reason=None,
                           _closing_since=None,
                           _next_decision=self.clock() + next(r["rules"]["cooldown_seconds"] for r in state["strategies"] if r["id"] == bot["id"]))
                self.resources[state["id"]]["closing_deadlines"].pop(bot["id"], None)
                self.resources[state["id"]]["hold_deadlines"].pop(bot["id"], None)
                self.save(state)
                return
        if self.unwatched_entries(state, bot, api):
            return  # one of them exists after all: it is read and cancelled as usual before the settlement
        # Refresh terminal cumulative fees/PnL once more, then require matching bills and cash.
        for order in bot["_orders"]:
            known = order.get("result") or {}
            if order.get("not_created") or (known.get("state") in {"canceled", "mmp_canceled"} and not D(known.get("contracts", "0"))):
                # R2.1.6: nothing to settle, and OKX keeps a cancelled unfilled order only about 2 hours: its GET then
                # answers 51603 and the settlement failed every step, the season never DONE. Orders with fills are read.
                continue
            result = protection.result_for(api, bot, order)
            with self.lock:
                engine.record_result(state, bot, order, result, self.clock(), seen_by="closing")
        if api.position() or api.pending():
            raise PreflightError("terminal_account_not_flat")
        bills, balance = api.bills(bot["_begin_ms"]), api.balance()
        rows = [{k: row[k] for k in BILL_FIELDS if k in row} for row in bills if isinstance(row, dict)]
        digest = fingerprint({"bills": rows, "balance": dict(balance)})
        if bot.get("_accounting_evidence_sha256") != digest:
            with self.lock:  # kept before the strict check, so a mismatch keeps its evidence too; once per distinct set
                parts = max(1, -(-len(rows) // BILLS_PER_EVENT))
                for part in range(parts):
                    emit(state, self.clock(), bot, 6, "accounting_evidence", "Сирі OKX bills і кінцевий баланс, на яких тримається звірка.",
                         bills=rows[part * BILLS_PER_EVENT:(part + 1) * BILLS_PER_EVENT], part=part + 1, parts=parts,
                         bills_count=len(rows), evidence_sha256=digest, balance=dict(balance), read_at=self.clock())
                bot["_accounting_evidence_sha256"] = digest
                self.save(state)
        with self.lock:
            engine.settle(state, bot, bills, balance)
            bot["status"] = "STOPPED"
            emit(state, self.clock(), bot, 6, "accounting_confirmed", "Позиція нульова; ордери, funding, комісії та зміна USDT звірені.", **bot["accounting"],
                 bills_reconciled=bot["accounting"]["bill_count"], cash_delta_reconciled=bot["accounting"]["cash_delta_matches"])
            self.save(state)

    def step(self, state):
        """One iteration; R2.1.10 (C): its duration goes into step_timing, about hourly and once at the end."""
        self.execution_owner_guard()  # a handed-over backend refuses before anything else
        began = self.monotonic()
        result = self._step(state)
        self.note_step(state, self.monotonic() - began)
        return result

    def note_step(self, state, seconds):
        resource = self.resources.get(state["id"])
        if resource is None:
            return
        timing = resource.setdefault("step_seconds", [])
        timing.append(seconds)
        if state["status"] == "PAUSED" and not resource.get("timing_final"):
            resource["timing_final"] = True
            with self.lock:  # R2110-TIMING-DONE: the PAUSED season's final report; a failed save leaves it PAUSED as it is
                self.report_timing(state, resource)
                try:
                    self.save(state)
                except (ArenaError, OSError, StorageBlocked):
                    pass
        elif len(timing) >= STEP_TIMING_EVERY and state["status"] != "DONE":
            with self.lock:
                self.report_timing(state, resource)  # journalled by the next step's save: no save of its own here

    def report_timing(self, state, resource):
        """step_timing (under the lock): step quantiles since the last report, request counters, every calibration RTT."""
        timing, apis = resource.setdefault("step_seconds", []), resource["apis"]
        ordered = sorted(timing)
        quantile = lambda p: round(ordered[min(len(ordered) - 1, int(p * len(ordered)))], 3) if ordered else None
        emit(state, self.clock(), None, 1, "step_timing", "Тривалість кроків і лічильники запитів OKX.",
             steps=len(ordered), step_p50_seconds=quantile(.5), step_p90_seconds=quantile(.9),
             step_max_seconds=round(ordered[-1], 3) if ordered else None, account_requests=[getattr(a, "requests", None) for a in apis],
             account_writes=[getattr(a, "write_count", None) for a in apis], public_gets=self.public_gets(resource, None),
             calibration_rtt_ms=[list(getattr(a, "calibration_rtts", [])) for a in apis])
        for api in apis:
            if isinstance(getattr(api, "calibration_rtts", None), list):
                api.calibration_rtts.clear()
        timing.clear()

    def _step(self, state):
        self.execution_owner_guard()
        resource = self.resources[state["id"]]
        stamp, snapshot = self.clock(), None
        phase = "fetch"
        with self.lock:
            expired = stamp >= state["_end"] or self.monotonic() >= resource["deadline"]
            if expired and draining_policy(state):
                close_admission(state, stamp, emit)
            elif expired:
                engine.stop(state, stamp, reason="Час сезону завершено.", code="deadline")
            if state["_ticks"] >= state["rules"].get("max_observations", engine.LIMITS["max_observations"]):
                state["incomplete"] = True
                if state.get("protocol_version", 0) >= 8:
                    state["admission_blocked"] = "capacity"
                    close_admission(state, stamp, emit)
                else: engine.stop(state, stamp, reason="Ліміт спостережень сезону завершено.", code="observation_limit")
            state["_ticks"] += 1
            resource["health"].mark("loop", self.monotonic())
        for bot, api in zip(state["bots"], resource["apis"]):
            api.recovery_transport = bool(bot["stop_requested"] or bot.get("_exit_requested"))
        # One GET task per independent account; no concurrent writes or shared API instance. The account checks come
        # before the market snapshot, so a slow account API (OKX Demo: ~25 s a check) never ages the quote decided on.
        futures = {bot["id"]: resource["pool"].submit(self.timed_account_check, state, bot, api)
                   for bot,api in zip(state["bots"],resource["apis"]) if bot["status"] != "STOPPED" and not bot["_manual_review"]}
        accounts = {}
        for identity, future in futures.items():
            try:
                accounts[identity] = future.result()
            except (PreflightError, ArenaError, OSError, ValueError, KeyError, TypeError) as error:
                accounts[identity] = error
        mixed = engine.mixed(state)
        if mixed:  # R2.1.12 (MIX): one snapshot per contract; each bot is judged on its own contract's only
            snapshots = self.observe_markets(state, resource, stamp)
        else:
            snapshot = self.observe_market(state, resource, stamp)
        candidates = []
        for bot, api in zip(state["bots"], resource["apis"]):
            if bot.get("_degraded") and (bot["status"] == "STOPPED" or bot["_manual_review"]) and not bot.get("_settled_check"):
                with self.lock:  # R2.1.14 RC2 (RC-04): a settled bot's read ends its own window (settled_check)
                    self.end_window(state, bot, "stopped")
            if bot.get("_settled_check") and bot["status"] == "STOPPED":
                self.settled_check(state, resource, bot, api)
            if bot["status"] == "STOPPED" or bot["_manual_review"] or bot["id"] not in accounts:
                continue
            if mixed:
                snapshot = snapshots.get(engine.instrument_for(state, bot["id"]))
            try:
                own_snapshot(state, bot, snapshot)  # R2.1.12 (MIX, plan check p. 3), the failed check's A3 path included (X1-06)
                if isinstance(accounts[bot["id"]], Exception):
                    raise accounts[bot["id"]]
                with self.lock:
                    bot.pop("admission_blocked", None)
                    self.end_window(state, bot, "recovered")  # A4: a run of failed checks ends with this one
                    recipe = next(r for r in state["strategies"] if r["id"] == bot["id"])
                    exiting = self.check_exits(state, resource, bot, recipe, snapshot)
                    if not exiting and snapshot and bot["position_btc"] and not bot.get("_exit_requested") and not bot["stop_requested"]:
                        managed = runtime.management(recipe, bot, snapshot)
                        if managed.get("reduce") and engine.intent_count(bot) >= engine.LIMITS["max_order_intents_per_bot"] - 20:
                            bot["_next_decision"] = self.clock() + 30
                            emit(state, self.clock(), bot, 5, "reduction_veto", "Резерв заявок потрібен для повного виходу; часткове скорочення пропущено.", reason="close_intent_reserve")
                        elif managed.get("reduce"):
                            lot = D(bot["_instrument"]["lot_step"])
                            quantity = (abs(D(bot["_contracts"])) * D(str(managed["reduce_pct"])) / 100 / lot).to_integral_value(rounding="ROUND_DOWN") * lot
                            if quantity >= D(bot["_instrument"]["minimum_order_units"]) and quantity < abs(D(bot["_contracts"])):
                                runtime.reserve_reduction(recipe, bot, snapshot, self.identity_for(state, bot, "C"))
                                bot.update(_reduce_remaining=str(quantity), _exit_requested=True, _close_reason="Формальне часткове закриття.", status="CLOSING", _closing_since=self.clock())
                                emit(state, self.clock(), bot, 5, "reduction_trigger", "Стратегія скорочує власну позицію; залишок продовжує цикл.", reduce_pct=managed["reduce_pct"], contracts=str(quantity), snapshot_sha256=snapshot["sha256"], bar_time_ms=snapshot.get("bar_close_ms"), checks=managed.get("reduce_checks"))
                                self.save(state)
                    must_close = bot["stop_requested"] or bot.get("_exit_requested")
                if must_close:
                    self.closing(state, bot, api)
                elif snapshot and (v4rt.decision_due(bot, snapshot) if v4rt.enabled(recipe) and not bot["position_btc"]
                                   else self.clock() >= bot["_next_decision"]) and not engine.unresolved(bot) and not self.unseen_hold(state, bot):
                    with self.lock:
                        cand = engine.candidate(state, bot, recipe, snapshot, self.clock())
                        if v4rt.enabled(recipe) and not bot["position_btc"]:
                            v4rt.decided(bot, snapshot)  # D106: one decision per newly visible bar, not the 30 s grid
                        else:
                            bot["_next_decision"] = self.clock() + 30
                        emit(state, self.clock(), bot, 4 if bot["position_btc"] else 1, "position_checks" if bot["position_btc"] else "entry_checks",
                             ("Формальні умови й обмеження пройдено; перед виконанням повторно перевіряємо дані, без Jev."
                              if cand["local_checks_passed"] and state.get("execution_mode") in {demo_execution.MODE, exchange_execution.RULES_ONLY}
                              else check_summary(cand)), checks=cand["checks"], kind=cand["kind"], local_checks_passed=cand["local_checks_passed"],
                             snapshot_sha256=snapshot["sha256"], bar_time_ms=snapshot.get("bar_close_ms"),
                             observed_at=snapshot["at"], server_time=snapshot["server_time"], quote_ts=snapshot["quote_ts"])
                        bot["status"] = "HOLDING" if bot["position_btc"] else "WAITING"
                        if cand["local_checks_passed"] and not state["_recovery"]:
                            if state.get("execution_mode") == "shared_jev":
                                bot["status"] = "JEV_PENDING"
                            candidates.append(cand)
                        else:
                            emit(state, self.clock(), bot, 5 if bot["position_btc"] else 2, "no_signal", "Утримуємо/очікуємо; Jev не витрачається без сигналу.")
                        self.save(state, force=False)
                bot["_api_failures"] = 0
            except (PreflightError, ArenaError, OSError, ValueError, KeyError, TypeError) as error:
                if str(error) in {"stop_fire_pending", "stop_fire_recovery"}:
                    continue  # R2.1.14: our own stop's facts are on their way (or its recovery began): no account failure, no A3
                if mixed and str(error) == "ticker_instrument_mismatch":
                    snapshot = None  # never another contract's quote, not even for the A3 window
                elif mixed and snapshot and D(bot["_contracts"]) and not fresh_market(snapshot, self.clock()):
                    snapshot = self.reread_contract(state, resource, bot, snapshots)
                if str(error) == "actual_position_differs_from_owned_fills" and leveraged(state):
                    self.liquidation_seen(state, bot, api)  # R2.1.15: liquidation or ADL rows first; the manual review follows
                with self.lock:
                    close_now = self.account_failure(state, resource, bot, error, snapshot)
                    self.save(state)
                if close_now:  # A3: an exit fired inside the extension: the ordinary close order at once
                    try:
                        self.closing(state, bot, api)
                    except (PreflightError, ArenaError, OSError, ValueError, KeyError, TypeError) as again:
                        with self.lock:
                            self.account_failure(state, resource, bot, again, snapshot, exits=False)
                            self.save(state)
        if candidates:
            with self.lock: self.save(state)
            if mixed:  # R2.1.12 (MIX, plan check p. 1): one decision, one SHADOW payload per contract, on its own snapshot
                for contract in sorted(snapshots):
                    group = [c for c in candidates if engine.instrument_for(state, c["id"]) == contract]
                    if group:
                        self.decide(state, resource, group, snapshots[contract])
            else:
                self.decide(state, resource, candidates, snapshot)
        with self.lock:
            state["jev_usage"] = self.ledgers[state["context"]].summary(state["id"])
            if state["jev_usage"]["unknown_billing"]:
                state["incomplete"] = True
                for bot in state["bots"]:
                    if bot["id"] != "cash":
                        bot["ai_cost_complete"] = False
            done = all(b["status"] == "STOPPED" and not b.get("_settled_check") for b in state["bots"] if b["id"] != "cash")
            if done:
                for bot in state["bots"]:  # R2110-WINDOW-END: a window still open ends with the season
                    if bot.get("_degraded"):
                        self.end_window(state, bot, "stopped")
                self.report_timing(state, resource)  # R2110-TIMING-DONE: the final report before season_done, saved with it
                state["incomplete"] = state["incomplete"] or any(not b["accounting_reconciled"] or not b["ai_cost_complete"] for b in state["bots"])
                state.update(status="DONE", finished_at=iso(self.clock()))
                state["bots"][-1]["status"] = "DONE"
                state["winner_name"] = None if state["incomplete"] else winner_name(state)
                emit(state, self.clock(), None, 6, "season_done", "Біржовий сезон завершено; залишки і звіт звірені.", incomplete=state["incomplete"])
            else:
                # Individual Stop and ordinary rule exits share the same bounded recovery.
                # Monotonic deadlines prevent a wall-clock correction extending that window.
                overdue = False
                for bot in state["bots"]:
                    if bot["id"] == "cash" or bot["status"] == "STOPPED":
                        continue
                    if not (bot["stop_requested"] or bot.get("_exit_requested")):
                        resource["closing_deadlines"].pop(bot["id"], None)
                        continue
                    age = max(0, self.clock() - bot.get("_closing_since", self.clock()))
                    deadline = resource["closing_deadlines"].setdefault(bot["id"], self.monotonic() + max(0, 120 - age))
                    late = age >= 120 or self.monotonic() >= deadline
                    waiting = engine.unresolved(bot)
                    if (late and not bot["_manual_review"] and not state.get("_recovery") and not bot.get("_protection_recovery")
                            and not D(bot["_contracts"]) and waiting and all((o.get("_absent") or {}).get("nothing_at") for o in waiting)):
                        # R219-STOP-SLOW: a flat bot whose unresolved orders only wait for their two-phase verdict gets one
                        # more account check (slow private requests can put the marking step past the deadline). R2110-STUCK
                        # (D138): never in manual review (it gets no check) or a recovery (a Resume, the protection's
                        # recovery); for a Stop at most VERDICT_GRACE_SECONDS past the deadline; this process only
                        checked = resource.get("account_checked_at", {}).get(bot["id"], 0)
                        wall, mono = resource.setdefault("verdict_grace", {}).setdefault(bot["id"], (self.clock(), self.monotonic()))
                        late = checked > wall or self.monotonic() - mono >= VERDICT_GRACE_SECONDS
                    overdue = overdue or late
                if overdue:
                    engine.stop(state, self.clock(), reason="Закриття/звірка одного з ботів не завершені за120с.", code="close_timeout")
                    state.update(status="PAUSED", incomplete=True, error="Журнал і фактичні залишки збережені; потрібне відновлення закриття.")
                    emit(state, self.clock(), None, 6, "closing_paused", state["error"])
            if mixed:
                count = len(state["frames"])
                frame(state, self.clock(), None)  # market: null, as for a market fault (arena_engine is under the hash)
                if len(state["frames"]) > count:  # R2.1.12 (MIX, plan check p. 11): only a frame just added
                    state["frames"][-1]["markets"] = {c: {"mid": s["mid"]} if s else None for c, s in sorted(snapshots.items())}
            else:
                frame(state, self.clock(), snapshot)
            resource["health"].mark("checkpoint", self.monotonic(), durable_seq=self.event_positions.get(state["id"], 0))
            # ops_health is the operations heartbeat (/api/ops), not an entry gate: its new_entry_permitted is advisory.
            # Gating on it would stop every OKX Demo entry (an account check takes ~25 s, over the 15 s loop limit). The
            # step enforces admission itself: account checks before the quote, a quote at most 10 s old, the clock guard,
            # admission_blocked on market, clock and storage faults, and the rolling read budget whose critical headroom
            # serves own-position reads only (the manifest's rolling_health_blocks_admission_only).
            state["ops_health"] = resource["health"].status(self.monotonic())
            state["phase"] = public_phase(state)
            self.save(state, force=state["status"] in {"DONE", "PAUSED"})
        if state["status"] in {"RUNNING", "DRAINING", "CLOSING"} and any(
                o["request"]["ordType"] == "post_only" and not o["request"]["reduceOnly"]
                for bot in state["bots"] if bot["id"] != "cash" for o in engine.unresolved(bot)):
            self.watch_fills(state, "step_end")  # a fill during the step is protected now, not after the next idle slice
        for bot, api in zip(state["bots"], resource["apis"]):  # no worker watches a PAUSED season or a manual-review bot
            if state["status"] == "PAUSED" or bot["_manual_review"]:
                self.cancel_resting(state, bot, api, "paused" if state["status"] == "PAUSED" else "manual_review",
                                    final=state["status"] == "PAUSED")

    def cancel_due(self, state, order, final=False):
        """A repeated post-only cancel is allowed MAX_CANCEL_ATTEMPTS times per worker run, CANCEL_RETRY_SECONDS apart; the
        last chance before a pause gets one more attempt without the spacing."""
        count, last = ((self.resources.get(state["id"]) or {}).get("cancel_tries") or {}).get(order["request"]["clOrdId"], (0, float("-inf")))
        return count < MAX_CANCEL_ATTEMPTS + final and (final or self.clock() - last >= CANCEL_RETRY_SECONDS)

    def count_cancel(self, state, order):
        """Call under the lock, just before the cancel POST."""
        tries = (self.resources.get(state["id"]) or {}).setdefault("cancel_tries", {})
        count, _ = tries.get(order["request"]["clOrdId"], (0, float("-inf")))
        tries[order["request"]["clOrdId"]] = (count + 1, self.clock())
        order.update(cancel_requested=True, cancel_sent_at=self.clock(), cancel_attempts=order.get("cancel_attempts", 0) + 1)

    def cancel_resting(self, state, bot, api, reason, final=False):
        """Best effort when nothing will watch a bot (PAUSED, manual review, worker failure): its resting post-only entry is
        cancelled on the venue and re-read, so it cannot fill unprotected meanwhile. The last chance before a pause skips
        the retry spacing; a Resume starts a fresh allowance and repeats the cancel if the order is still live. A
        handed-over workspace sends nothing. Never raises; call it without holding the state lock."""
        try:
            self.execution_owner_guard()
        except ArenaError:
            return
        for order in engine.unresolved(bot):
            if (order["request"]["ordType"] != "post_only" or order["request"]["reduceOnly"]
                    or not self.cancel_due(state, order, final)):
                continue
            with self.lock:
                self.count_cancel(state, order)
            sent, outcome = self.clock(), "acknowledged"
            try:
                api.cancel(order["request"]["clOrdId"])
            except (PreflightError, OSError, ValueError):
                outcome = "uncertain"  # the GET below decides
            with self.lock:
                emit(state, self.clock(), bot, 3, "exchange_cancel_posted", "POST скасування заявки: час надсилання й відповіді біржі.",
                     client_order_id=order["request"]["clOrdId"], outcome=outcome, post_seconds=round(self.clock() - sent, 3))
            try:
                result = protection.result_for(api, bot, order)
                with self.lock:
                    engine.record_result(state, bot, order, result, self.clock(), seen_by="cancel_check")
            except (PreflightError, OSError, ValueError, KeyError, TypeError):
                result = None
            with self.lock:
                emit(state, self.clock(), bot, 3, "entry_order_unwatched_cancel",
                     "Бот без нагляду: post-only заявку скасовано на біржі." if result and result["state"] in TERMINAL else
                     "Бот без нагляду: скасування post-only заявки не підтверджено; Resume перечитає й повторить.",
                     reason=reason, client_order_id=order["request"]["clOrdId"], attempts=order["cancel_attempts"],
                     order_state=result["state"] if result else "unknown")
                try:
                    self.save(state)
                except (ArenaError, OSError, StorageBlocked):
                    pass  # the event stays in the in-memory journal; the pause handler's own save records the outcome

    def decide(self, state, resource, candidates, snapshot):
        """In a mixed season (R2.1.12, MIX) every candidate trades one contract and snapshot is that contract's."""
        self.execution_owner_guard()
        contract = engine.instrument_for(state, candidates[0]["id"]) if engine.mixed(state) else None
        tag = {"instrument": contract} if contract else {}
        with self.lock:
            stopped = [c for c in candidates if next(b for b in state["bots"] if b["id"] == c["id"])["stop_requested"]]
            if stopped:
                self.cancel_entries(state, stopped, "owner_stop", snapshot)
            candidates = [c for c in candidates if c not in stopped]
            if not candidates:
                if stopped:
                    self.save(state)
                return
            paid = state.get("execution_mode") not in {demo_execution.MODE, exchange_execution.RULES_ONLY}  # a missing mode is shared Jev
            budget = PAID_DECISION_MAX_AGE_SECONDS if paid else RULES_DECISION_MAX_AGE_SECONDS
            why = ("season_not_running" if state["status"] != "RUNNING" else "admission_ended" if self.clock() >= state["_end"]
                   else "season_deadline" if self.monotonic() >= resource["deadline"]
                   else "decision_snapshot_too_old" if self.clock() - snapshot["at"] > budget else None)
            if why:
                self.cancel_entries(state, candidates, why, snapshot)
                self.save(state)
                return
        mechanics = state.get("execution_mode") == demo_execution.MODE
        rules_only = state.get("execution_mode") == exchange_execution.RULES_ONLY
        if mechanics:
            outcome = {"attempted": False, "reported_cost_usd": 0, "answers": {}, "elapsed_ms": 0,
                       "reason": "pinned_demo_rules", "allowed": {c["id"] for c in candidates if demo_execution.allows(state, c)}}
        elif rules_only:
            outcome = exchange_execution.decision(state, candidates)
            with self.lock:
                emit(state, self.clock(), None, 2, "jev_shadow_payload",
                     "Точний payload записано для офлайн SHADOW; модель не викликалась і не керувала ордерами.",
                     payload=outcome["payload"], payload_wire_json=outcome["payload_wire_json"],
                     payload_sha256=outcome["payload_sha256"], payload_content_sha256=outcome["payload_content_sha256"],
                     candidate_ids=[c["id"] for c in candidates], request_at_ms=int(self.clock()*1000),
                     snapshot_sha256=snapshot["sha256"], observed_at=snapshot["at"],
                     bar_time_ms=snapshot.get("bar_close_ms"), ai_used=False, sent=False, affects_execution=False,
                     payload_bytes=outcome["payload_bytes"], **tag)
                outcome["shadow_event_seq"] = state["events"][-1]["seq"]
                self.save(state)
        else:
            outcome = resource["gate"].decide(state["id"], candidates)
        with self.lock:
            for cand in candidates:
                bot = next(b for b in state["bots"] if b["id"] == cand["id"])
                bot["ai_requests"] += int(outcome["attempted"])
                if outcome["reported_cost_usd"] is not None:
                    bot["ai_cost_usd"] += outcome["reported_cost_usd"] / len(candidates)
                elif outcome["attempted"]:
                    bot["ai_cost_complete"] = False
                    state["incomplete"] = True
                bot["status"] = "CLOSING" if bot["stop_requested"] else "HOLDING" if bot["position_btc"] else "WAITING"
                if mechanics:
                    emit(state, self.clock(), bot, 2, "demo_execution_review",
                         "Перевірка механіки без Jev: дозвіл повторно перевірити вхід." if bot["id"] in outcome["allowed"] else
                         "Перевірка механіки без Jev: дія заблокована умовами тесту.",
                         execution_mode=demo_execution.MODE, ai_used=False, kind=cand["kind"],
                         rules_allowed=bot["id"] in outcome["allowed"], snapshot_sha256=snapshot["sha256"],
                         observed_at=snapshot["at"], bar_time_ms=snapshot.get("bar_close_ms"))
                    continue
                if rules_only:
                    emit(state, self.clock(), bot, 5 if cand["kind"] == "add" else 2, "rules_only_review",
                         "Формальний рецепт дозволив повторну перевірку входу/докупівлі." if bot["id"] in outcome["allowed"] else
                         "Формальний рецепт не дозволив дію; нового ордера немає.",
                         execution_mode=exchange_execution.RULES_ONLY, ai_used=False, kind=cand["kind"],
                         rules_allowed=bot["id"] in outcome["allowed"], snapshot_sha256=snapshot["sha256"],
                         observed_at=snapshot["at"], bar_time_ms=snapshot.get("bar_close_ms"),
                         shadow_payload_sha256=outcome["payload_sha256"])
                    continue
                answer = outcome["answers"].get(bot["id"])
                jev_allowed = bot["id"] in outcome["allowed"]
                low_confidence = bool(answer and answer["confidence"] < CONFIDENCE_THRESHOLD)
                blocked_reason = None if jev_allowed else "low_confidence" if low_confidence else (
                    "choice_hold" if answer and answer["choice"] == "hold" else outcome["reason"])
                message = (f"Jev: {answer['choice']}, confidence {answer['confidence']} < {CONFIDENCE_THRESHOLD:.2f}; дія заблокована."
                           if low_confidence else "Jev: " + (
                               "перевірка утримання; ордера немає." if cand["kind"] == "hold" else
                               "дозвіл повторно перевірити докупівлю." if cand["kind"] == "add" and jev_allowed else
                               "дозвіл перевірити вхід." if jev_allowed else "утримати/очікувати."))
                emit(state, self.clock(), bot, 5 if cand["kind"] in {"add", "hold"} else 2, "jev_decision",
                     message, jev_allowed=jev_allowed, blocked_reason=blocked_reason,
                     confidence_threshold=CONFIDENCE_THRESHOLD,
                     kind=cand["kind"], test_plan=cand.get("test_plan"),
                     answer=answer, reason=outcome["reason"],
                     elapsed_ms=outcome["elapsed_ms"], reported_batch_cost_usd=outcome["reported_cost_usd"],
                     cost_allocation="equal_share_estimate", ai_used=outcome["attempted"],
                     payload_sha256=outcome.get("payload_sha256"), request_at_ms=outcome.get("request_at_ms"),
                     reply_at_ms=int(self.clock()*1000), snapshot_sha256=snapshot["sha256"],
                     observed_at=snapshot["at"], bar_time_ms=snapshot.get("bar_close_ms"))
                if cand["kind"] == "hold":
                    bot["_probe_hold_attempted"] = True
                    answer = outcome["answers"].get(bot["id"])
                    bot["_probe_hold_checked"] = bool(outcome["reason"] == "typed_answer" and answer and
                                                      answer["confidence"] >= CONFIDENCE_THRESHOLD)
            self.save(state)
        allowed = [c for c in candidates if c["kind"] != "hold" and c["id"] in outcome["allowed"]]
        decision_budget = RULES_DECISION_MAX_AGE_SECONDS if mechanics or rules_only else JEV_DECISION_MAX_AGE_SECONDS
        late = ("decision_too_slow" if outcome["elapsed_ms"] > 8000 else
                "decision_snapshot_too_old" if self.clock() - snapshot["at"] > decision_budget else None)
        if not allowed or late:
            if allowed:
                with self.lock:
                    self.cancel_entries(state, allowed, late, snapshot)
                    self.save(state)
            return
        # Fresh account AND market checks after either decision; never execute on its old quote. Each bot's account
        # checks run in parallel (one GET task per account), and the fresh public snapshot is taken after them, so a
        # slow account API (OKX Demo) cannot age the quote the order is judged on.
        bots = {c["id"]: next(b for b in state["bots"] if b["id"] == c["id"]) for c in allowed}
        def check(bot):
            return self.timed_account_check(state, bot, resource["apis"][state["bots"].index(bot)])
        done = resource.setdefault("account_checked_at", {})
        stale = [c for c in allowed if self.clock() - done.get(c["id"], float("-inf")) > ACCOUNT_CHECK_MAX_AGE_SECONDS]
        checks = {c["id"]: resource["pool"].submit(check, bots[c["id"]]) for c in stale}
        checked = {c["id"]: done[c["id"]] for c in allowed if c["id"] not in checks}
        for cand in stale:
            try:
                checked[cand["id"]] = checks[cand["id"]].result()
            except (PreflightError, ArenaError, OSError, ValueError, KeyError, TypeError) as error:
                checked[cand["id"]] = error
        fresh = None
        for cand in allowed:
            bot = bots[cand["id"]]
            api = resource["apis"][state["bots"].index(bot)]
            try:
                if isinstance(checked[cand["id"]], Exception):
                    raise checked[cand["id"]]
                if self.clock() - checked[cand["id"]] > ACCOUNT_CHECK_MAX_AGE_SECONDS:  # an earlier order took time
                    checked[cand["id"]] = check(bot)
                    fresh = None
                if fresh is None:
                    requested = self.clock()  # the account checks used must have finished within 10 s before this
                    fresh = self.fresh_snapshot(state, resource, [cand], snapshot, contract)
                    if fresh is None:
                        continue
                own_snapshot(state, bot, fresh)  # R2.1.12 (MIX, plan check p. 3)
                with self.lock:
                    engine.mark(state, bot, fresh)
                    recipe = next(r for r in state["strategies"] if r["id"] == bot["id"])
                    again = engine.candidate(state, bot, recipe, fresh, self.clock())
                    blockers = [] if again["local_checks_passed"] else entry_blockers(state, bot, again, self.clock())
                    exit_now = engine.exit_reason(state, bot, recipe, fresh, self.clock())
                    ages = {"decision_age_seconds": round(self.clock() - snapshot["at"], 3),
                            "fresh_age_seconds": round(self.clock() - fresh["at"], 3),
                            "quote_age_seconds": round(self.clock() - fresh["quote_ts"] / 1000, 3),
                            "account_check_age_seconds": round(self.clock() - checked[cand["id"]], 3),
                            "account_check_before_quote_seconds": round(requested - checked[cand["id"]], 3)}
                    reasons = list(dict.fromkeys(name for name, failed in (
                        *((name, True) for name in blockers),
                        ("kind_changed", again["kind"] != cand["kind"]),
                        ("exit_condition", bool(exit_now)), ("signal_changed", again.get("signal_id") != cand.get("signal_id")),
                        ("side_changed", again["side"] != cand["side"]),
                        ("decision_snapshot_too_old", ages["decision_age_seconds"] > decision_budget),
                        ("fresh_quote_too_old", ages["fresh_age_seconds"] > FRESH_QUOTE_MAX_AGE_SECONDS),
                        ("account_check_too_old", ages["account_check_before_quote_seconds"] > ACCOUNT_CHECK_MAX_AGE_SECONDS
                         or ages["account_check_age_seconds"] > ACCOUNT_CHECK_HARD_MAX_AGE_SECONDS)) if failed))
                    if reasons:
                        emit(state, self.clock(), bot, 1, "entry_cancelled", "Після свіжої перевірки умови/час/Stop змінилися: ордера немає.",
                             kind=cand["kind"], reason=reasons[0], reasons=reasons, checks=again["checks"],
                             execution_snapshot_sha256=fresh["sha256"], decision_snapshot_sha256=snapshot["sha256"], **ages)
                        self.save(state)
                        continue
                    # The pinned Demo scope only once the ordinary fresh reasons are clear: a market change (spread) is
                    # a step-1 cancel with its checks, not a "checks unfinished" fault (Demo 4a6e4af6, R2.1.4).
                    if mechanics and not demo_execution.allows(state, again):
                        raise PreflightError("demo_mechanics_entry_scope")
                    body = engine.entry_body(state, bot, recipe, fresh, self.identity_for(state, bot, "E"), cand.get("maker"))
                    if contract:
                        state.setdefault("_last_by_instrument", {})[contract] = {k: deepcopy(v) for k, v in fresh.items() if k != "raw"}
                    else:
                        state["_last"] = {k: deepcopy(v) for k,v in fresh.items() if k != "raw"}
                    emit(state, self.clock(), bot, 1, "entry_rechecked", "Свіжі дані й акаунт повторно перевірено перед входом.",
                         checks=again["checks"], execution_snapshot_sha256=fresh["sha256"], **ages,
                         **maker_evidence(cand.get("maker"), snapshot, fresh),
                         **({"shadow_payload_sha256": outcome["payload_sha256"], "shadow_event_seq": outcome["shadow_event_seq"]} if rules_only else {}),
                         **({"rules_snapshot_sha256": snapshot["sha256"], "execution_mode": state["execution_mode"]} if mechanics or rules_only else
                            {"jev_snapshot_sha256": snapshot["sha256"]}))
                    self.save(state)
                fresh = None  # the next candidate is judged on a quote taken after this order
                if self.submit(state, bot, api, body, self.clock()) is False:
                    with self.lock:
                        why = ("owner_stop" if bot["stop_requested"] else "season_not_running" if state["status"] != "RUNNING"
                               else "admission_ended" if self.clock() >= state["_end"] else "season_deadline")
                        self.cancel_entries(state, [cand], why, snapshot)
                        self.save(state)
            except (PreflightError, ArenaError, OSError, ValueError, KeyError, TypeError) as error:
                with self.lock:
                    diagnostic = exchange_diagnostics.failure(error, stage="account_check", fallback="entry_final_checks_failed")
                    reason = str(error) if isinstance(error, PreflightError) and str(error) in {
                        "minimum_lot_liquidity_or_exposure_veto", "stop_loss_notional_veto", "diagnostic_notional_or_intent_cap", "diagnostic_demo_only",
                        "demo_mechanics_entry_scope", "post_only_would_cross", "post_only_limit_unknown"} else "entry_final_checks_failed"
                    emit(state, self.clock(), bot, 3, "entry_cancelled", "Перевірки або відповідь не завершені; надіслані заявки та fills звіряються за журналом.",
                         kind=cand["kind"], reason=reason, reasons=[reason] + ([diagnostic["reason"]] if diagnostic["reason"] != reason else []),
                         diagnostic=diagnostic["reason"])
                    self.save(state)

    def timed_account_check(self, state, bot, api):
        """checked_account, remembering when it started and finished: an entry may rely on it for 10 s."""
        started = self.clock()
        self.checked_account(state, bot, api)
        finished = self.clock()
        self.resources[state["id"]].setdefault("account_checked_at", {})[bot["id"]] = finished
        self.resources[state["id"]].setdefault("account_check_started_at", {})[bot["id"]] = started
        return finished

    def unseen_hold(self, state, bot):
        """R219-UNSEEN-HOLD: True while no account check that started after the bot's unseen verdict has finished."""
        held = bot.get("_unseen_hold")
        if held is None:
            return False
        if self.resources[state["id"]].get("account_check_started_at", {}).get(bot["id"], held) > held:
            bot.pop("_unseen_hold")
            return False
        return True

    def reread_contract(self, state, resource, bot, snapshots):
        """R2.1.13 (plan check p. 2): a mixed season reads its contracts one after another, so a quote read first can pass the
        10 s of A3 only because of the other contracts' reads (measured GETs: 343-922 ms). A position whose account check failed
        gets its own contract's quote again, for itself and the contract's other bots; None if it cannot be had."""
        contract = engine.instrument_for(state, bot["id"])
        fresh = self.fresh_snapshot(state, resource, [], snapshots[contract], contract)
        if fresh is not None:
            snapshots[contract] = fresh
        return fresh

    def fresh_snapshot(self, state, resource, candidates, snapshot, contract=None):
        """A new public snapshot, durable in the journal before any order; None (with events) when it cannot be had.
        contract (R2.1.12, MIX): the candidates' contract in a mixed season; its feed, its tag and its journal."""
        try:
            fresh = (resource["feeds"][contract] if contract else resource["feed"]).snapshot()
            if contract:
                tag_snapshot(fresh, contract)
            with self.lock:
                self.note_market_quality(state, fresh, contract)
                self.record_snapshot(state, fresh, contract)
            return fresh
        except (PreflightError, ArenaError, OSError, ValueError, KeyError, TypeError) as error:
            with self.lock:
                self.cancel_entries(state, candidates, "fresh_snapshot_failed", snapshot, step=3, error=type(error).__name__)
                self.save(state)
            return None

    def cancel_entries(self, state, candidates, reason, snapshot, step=1, **data):
        """One entry_cancelled per entry/add candidate with the exact reason; never a silent drop of a signal. A probe's
        hold review is not an entry and keeps its own diagnostics."""
        for cand in candidates:
            if cand["kind"] == "hold":
                continue
            bot = next(b for b in state["bots"] if b["id"] == cand["id"])
            emit(state, self.clock(), bot, step, "entry_cancelled", ENTRY_CANCELLED[reason], kind=cand["kind"], reason=reason,
                 reasons=[reason], decision_age_seconds=round(self.clock() - snapshot["at"], 3),
                 decision_snapshot_sha256=snapshot["sha256"], **data)

    def note_market_quality(self, state, snapshot, contract=None):
        status = snapshot.get("funding_status", "available")
        tag = {"instrument": contract} if contract else {}
        if contract:  # R2.1.12 (MIX): each contract's funding schedule
            statuses = state.setdefault("funding_status_by_instrument", {})
            previous, statuses[contract] = statuses.get(contract), status
        else:
            previous = state.get("_funding_status")
            state["_funding_status"] = status
        if status == "unknown_schedule" and previous != status:
            emit(state, self.clock(), None, 1, "demo_funding_unknown", DEMO_FUNDING_WARNING,
                 source=state["mode"], funding_status=status, accounting_source="OKX account bills",
                 snapshot_sha256=snapshot["sha256"], **tag)
        elif status == "available" and previous == "unknown_schedule":
            emit(state, self.clock(), None, 1, "funding_schedule_restored", "OKX знову повертає чинний календар funding.",
                 source=state["mode"], funding_status=status, **tag)

    def note_feature_book(self, state, feed, contract=None):
        """state["feature_book"]: the schema-4 book's warm-up, holes and requests for the panel, and a journal event when
        it starts, gets ready, opens or reads a hole, fails or gives up (not for every page or flapping secondary read).
        contract (R2.1.12, MIX): state["feature_books"][contract] and the event's instrument in a mixed season."""
        view = feed.book_state() if isinstance(feed, GapBookFeed) else None
        if (view is None or state["status"] != "RUNNING" or state.get("_recovery") or state.get("admission_blocked")
                or (state.get("admission_blocked_by_instrument") or {}).get(contract)):
            return  # the book does no work then (BookFeed.admitting): no warm-up to report on a Resume or after the cutoff
        if contract:
            books = state.setdefault("feature_books", {})
            previous, books[contract] = books.get(contract), view
        else:
            previous, state["feature_book"] = state.get("feature_book"), view
        def key(value):
            status = (value or {}).get("status")
            return ("ready" if status == "degraded" and not value.get("gap") else status, (value or {}).get("attempts"),
                    ((value or {}).get("gap") or {}).get("start_ms"), (value or {}).get("gaps_read"))
        if key(view) == key(previous):
            return
        gap, last = view["gap"], view["last_gap"]
        if view["status"] == "warming":
            message = (f"Книга ознак схеми 4 прогрівається: читаємо публічну історію хвилин (спроба {view['attempts']} з "
                       f"{view['warmup_attempts']}). До кінця прогріву рішень про вхід за схемою 4 немає.")
        elif view["status"] == "exhausted":
            message = (f"Прогрів книги ознак не вдався {view['warmup_attempts']} рази: до кінця раунду рішень про вхід за "
                       "схемою 4 немає. Відкриті позиції, стоп і виходи це не зачіпає.")
        elif view["status"] == "unavailable":
            message = ("Книга ознак тимчасово недоступна; повтор через 30 с." if view["warm"] else
                       f"Прогрів книги ознак не вдався (спроба {view['attempts']} з {view['warmup_attempts']}); повтор через 30 с.")
        elif gap:
            message = (f"Пропуск у хвилинах книги ознак: {gap['minutes']} хв. Дочитуємо їх із публічної історії; доти "
                       "рішень про вхід за схемою 4 немає.")
        elif last and last != (previous or {}).get("last_gap") and last.get("flat_filled"):
            # R2.1.11 (R219-BOOK-SPREAD): a flat minute has no volume, so spread features over it are UNKNOWN for their
            # whole period, as the evaluator has them (strategy_v4.py)
            message = ("Хвилину на межі вікна прочитати не вдалося: її заповнено пласко за попередньою ціною, як оцінювач "
                       "заповнює одиночну пропущену хвилину. Цінові ознаки рахуються далі; бари з цією хвилиною трохи відрізняються від справжніх."
                       + (" Spread-ознаки, що охоплюють цю хвилину, UNKNOWN на весь свій період, як в оцінювачі." if view.get("spread") else ""))
        elif last and last != (previous or {}).get("last_gap") and last["gave_up"] and last["read"] == last["minutes"]:
            message = (f"Свічки пропуску дочитано: {last['read']} з {last['minutes']} хв. Index і mark дочитуються окремо;"
                       " доти spread-ознаки над пропуском UNKNOWN.")
        elif last and last != (previous or {}).get("last_gap"):
            message = (f"Пропуск не вдалося дочитати: {last['read']} з {last['minutes']} хв. Ознаки, що охоплюють проміжок, "
                       "будуть UNKNOWN на весь свій період." if last["gave_up"] else
                       f"Пропуск дочитано: {last['read']} з {last['minutes']} хв."
                       + (f" Ще {last['missing']} хв немає в історії біржі: ознаки, що охоплюють проміжок, будуть UNKNOWN на"
                          " весь свій період." if last["missing"] > 1 else ""))
        else:
            message = "Книга ознак готова: рішення про вхід на кожному новому закритому барі."
        emit(state, self.clock(), None, 1, "feature_book", message, book_status=view["status"],
             warmup_attempt=view["attempts"], book_gets=view["gets"], bar_close_ms=view["bar_close_ms"],
             gap_minutes=(gap or last or {}).get("minutes"), gap_read=None if gap else (last or {}).get("read"),
             gap_missing=None if gap else (last or {}).get("missing"),
             gap_flat=None if gap else (last or {}).get("flat"),  # R2.1.10 (R219-GAP-MISSING): a new key
             **({"instrument": contract} if contract else {}))

    def record_snapshot(self, state, snapshot, contract=None):
        # Raw PUBLIC responses only, durable before an order. Account API responses stay private. A mixed season (R2.1.12,
        # MIX) keeps one journal per contract, each within the limit.
        path = engine.snapshot_files(self.path, state)[contract]
        limit = state["rules"].get("snapshot_journal_bytes", 64 * 1024 * 1024)
        record = json.dumps({"mode": state["mode"], "at": snapshot["at"], "sha256": snapshot["sha256"],
                            "funding_status": snapshot.get("funding_status", "available"),
                            "raw": snapshot.get("raw")}, separators=(",", ":"), allow_nan=False) + "\n"
        if (path.stat().st_size if path.exists() else 0) + len(record.encode("utf-8")) > limit:
            raise PreflightError("snapshot_journal_limit")
        with path.open("a", encoding="utf-8", newline="\n") as file:
            file.write(record)
            file.flush()
            os.fsync(file.fileno())
        state["public_get_count"] = self.public_gets(self.resources[state["id"]], 0)

    def public_gets(self, resource, default):
        """Public GETs of the season's feeds: the one feed's count, or the sum over a mixed season's (plan check p. 8)."""
        counts = [getattr(feed, "calls", default) for feed in resource["feeds"].values()]
        if len(counts) == 1:
            return counts[0]
        return None if any(c is None for c in counts) else sum(counts)

    def observe_markets(self, state, resource, stamp):
        """R2.1.12 (MIX): the snapshot of every contract that still has a bot to serve, one after another in this thread (one
        rolling budget, one IP). {contract: snapshot or None}; a contract whose bots are all STOPPED is not read, so its
        feed's fault cannot mark the others' round incomplete (D154 p. 2, X1-08). The ops market mark and the season's error
        only when every contract read is fresh. The wall-clock block is lifted once every feed that answered saw a clean
        observation after the jump: a dead feed holds no one up (D154 p. 2, X1-07), and when it answers again its own guard
        reports the jump."""
        resource["wall_clock_jumps"] = set()
        needed = sorted({engine.instrument_for(state, b["id"]) for b in state["bots"] if b["id"] != "cash" and b["status"] != "STOPPED"})
        snapshots = {c: self.observe_market(state, resource, stamp, c) for c in needed}
        with self.lock:
            if snapshots and all(snapshots.values()):
                state["error"] = None
                resource["health"].mark("market", self.monotonic())
            if state.get("admission_blocked") == "clock_gap" and any(snapshots.values()) and not resource["wall_clock_jumps"]:
                state.pop("admission_blocked", None)
        return snapshots

    def observe_market(self, state, resource, stamp, contract=None):
        """One public snapshot: of the season's contract (contract None: a one-contract season, as before R2.1.12) or of
        one contract of a mixed season, with its own feed, clock guard, failure count and entry block (D149 p. 3; plan
        check pp. 5-6: a wall-clock jump blocks the season, a gap or a quote regression the contract). None when the
        snapshot cannot be used: a returned-but-rejected snapshot must never reach candidates/Jev."""
        feed = resource["feeds"][contract] if contract else resource["feed"]
        tag = {"instrument": contract} if contract else {}
        phase = "fetch"
        try:
            snapshot = feed.snapshot()
            if abs(self.clock() - snapshot["at"]) > 10:
                raise PreflightError("market_arrival_stale")
            if self.timebase.synchronized and abs(self.clock() - snapshot["server_time"]) > 10:
                raise PreflightError("session_clock_source_disagreement")
            if contract:
                tag_snapshot(snapshot, contract)
            with self.lock:
                if state.get("protocol_version", 0) >= 8:
                    guard = resource["clock_guards"][contract] if contract else resource["clock_guard"]
                    health = guard.observe(wall=self.clock(), mono=self.monotonic(), quote_ms=snapshot["quote_ts"])
                    blocks = state.get("admission_blocked_by_instrument") or {}
                    if health["reasons"]:
                        state["incomplete"] = True
                        if not contract or "wall_clock_jump" in health["reasons"]:
                            state["admission_blocked"] = "clock_gap"
                        if contract and "wall_clock_jump" in health["reasons"]:
                            resource["wall_clock_jumps"].add(contract)
                        if contract and set(health["reasons"]) - {"wall_clock_jump"}:
                            state.setdefault("admission_blocked_by_instrument", {})[contract] = "clock_gap"
                        emit(state, self.clock(), None, 1, "clock_gap", "Зміна годинника/пропуск: нові входи заблоковано; відкриті позиції захищені.", **health, **tag)
                    elif contract:
                        if blocks.get(contract) in {"clock_gap", "market_unavailable"}:
                            blocks.pop(contract)
                    elif state.get("admission_blocked") in {"clock_gap", "market_unavailable"}:
                        state.pop("admission_blocked", None)
                    if "quote_regression" in health["reasons"]: raise PreflightError("market_gap_or_clock_regression")
                elif state["_last"] and (snapshot["quote_ts"] < state["_last"]["quote_ts"] or stamp - state["_last"]["at"] > 90):
                    raise PreflightError("market_gap_or_clock_regression")
                self.note_market_quality(state, snapshot, contract)
                self.note_feature_book(state, feed, contract)
                phase = "journal"
                try:
                    self.record_snapshot(state, snapshot, contract)
                except (OSError, PreflightError):
                    if state.get("protocol_version", 0) < 8: raise
                    state.update(incomplete=True, admission_blocked="snapshot_storage")
                    emit(state, self.clock(), None, 1, "recording_blocked", "Запис котирувань недоступний: входів немає; свіжа перевірена ціна лишається для виходу власної позиції.", reason="close_only", **tag)
                market = {"at": iso(snapshot["at"]), "bid": snapshot["bid"], "ask": snapshot["ask"],
                    "mid": snapshot["mid"], "features": snapshot["features"], "snapshot_sha256": snapshot["sha256"],
                    "source": snapshot.get("source"), "funding_status": snapshot.get("funding_status", "available")}
                if contract:
                    state.setdefault("_failures_by_instrument", {})[contract] = 0
                    state.setdefault("_last_by_instrument", {})[contract] = {k: deepcopy(v) for k, v in snapshot.items() if k != "raw"}
                    state.setdefault("latest_markets", {})[contract] = market
                else:
                    state.update(_failures=0, _last={k: deepcopy(v) for k, v in snapshot.items() if k != "raw"}, error=None)
                    state["latest_market"] = market
                self.save(state, force=False)
                if not contract:
                    resource["health"].mark("market", self.monotonic())
            return snapshot
        except (PreflightError, ArenaError, OSError, ValueError, KeyError, TypeError) as error:
            with self.lock:
                if contract:
                    failures = state.setdefault("_failures_by_instrument", {})
                    failures[contract] = failures.get(contract, 0) + 1
                    count = failures[contract]
                else:
                    state["_failures"] += 1
                    count = state["_failures"]
                diagnostic = market_diagnostic(error, feed)
                if phase == "journal" and isinstance(error, OSError):
                    diagnostic = {"stage": "journal", "reason": "snapshot_journal_write_failed",
                                  "message": "Не вдалося надійно зберегти журнал котирувань."}
                state["error"] = (contract + ": " + diagnostic["message"] + " Нових входів для ботів цього інструмента немає."
                                  if contract else diagnostic["message"] + " Нових входів немає.")
                state["public_get_count"] = self.public_gets(resource, 0)
                emit(state, self.clock(), None, 1, "market_unavailable", state["error"],
                     source=state["mode"], stage=diagnostic["stage"], reason=diagnostic["reason"],
                     consecutive_failures=count, public_get_count=state["public_get_count"], **tag)
                if count >= 3:
                    state["incomplete"] = True  # D138 p. 1, unchanged: a third failure of any contract's feed
                    if state.get("protocol_version", 0) >= 8:
                        if contract:
                            state.setdefault("admission_blocked_by_instrument", {})[contract] = "market_unavailable"
                        else:
                            state["admission_blocked"] = "market_unavailable"
                    else: engine.stop(state, self.clock(), reason="Перервано ринкові дані.", code="data_unavailable")
                self.save(state)
            return None

    def close(self):
        # Server shutdown must not invent a closed position or start a recovery worker.
        self.closed = True
        for thread in list(self.workers.values()):
            if thread.is_alive():
                return  # Non-daemon owner worker retains locks and continues its bounded run.
        for resource in self.resources.values():
            resource["stack"].close()
        self.resources.clear()
        if self.controller_owner is not None:
            self.controller_owner.__exit__(None, None, None)
            self.controller_owner = None
