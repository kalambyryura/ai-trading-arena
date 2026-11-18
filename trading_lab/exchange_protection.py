"""Owner-executor protective stop integration. Construction/GET never place orders."""
from copy import deepcopy
from contextlib import nullcontext
from decimal import Decimal
from .ops_policy import ProtectionIntent, owned_stop_intent, canonical, validate_stop_receipt
from .okx_readonly import PreflightError
from .exchange_api import ProtectionRejected
from .arena_engine import emit
from . import exchange_engine as engine
import hashlib


RECOVERY_POLICY = "owned-protection-close/v1"
# R2.1.15 (QR-01, D187): the rule of a never created order (exchange_lab.ABSENT_READS / ABSENT_SECONDS), for an owned stop
ABSENT_READS, ABSENT_SECONDS = 3, 60
REPLACEMENT_STOPS = 1  # RC2 (PP-01): stops OKX did not create replaced per open position; the next verdict is the recovery
STOP_PENDING = "/api/v5/trade/orders-algo-pending"


class ProtectionAbsencePending(PreflightError):
    """R2.1.15 (QR-01): OKX does not show an owned stop it never showed (51603) and the verdict is not due yet. No failure of
    the account check and no recovery: the bot takes no entry or add (its protection is unverified), its exits run."""


def absent_receipt(error):
    """OKX's 51603 ("order does not exist") on the GET of an owned stop's receipt."""
    return (getattr(error, "okx_code", None) == "51603" and getattr(error, "method", None) == "GET"
            and getattr(error, "endpoint", None) == "/api/v5/trade/order-algo")


def _lock(lab):
    # Legacy pure fixtures have no controller; production always supplies RLock.
    return getattr(lab, "lock", None) or nullcontext()


def _current(lab, state, bot, stored=None):
    """Never merge late I/O into another session/bot/protection record."""
    if (getattr(lab, "closed", False)
            or hasattr(lab, "sessions") and lab.sessions.get(state["id"]) is not state
            or not any(item is bot for item in state["bots"])):
        raise PreflightError("protection_owner_state_changed")
    if stored is not None and not any(item is stored for item in bot.get("_protections", [])):
        raise PreflightError("protection_record_changed")


def _token(lab, bot):
    return {"generation": getattr(lab, "data", {}).get("generation"),
            "owner": (bot.get("_account_tag"), bot.get("_main_tag")),
            "position": (bot["_contracts"], bot["entry_price"])}


def _close_only(api, state, bot):
    return (api.close_only or bot.get("stop_requested")
            or state.get("_recovery") or state["status"] not in {"RUNNING", "DRAINING"}
            or bot.get("_exit_requested") and bot.get("_reduce_remaining") is None)


def _recheck(lab, state, bot, api, token):
    """Under lock: refresh changed control generation, then revalidate eligibility.

    Stop may change generation while GET/POST is pending. Its latch wins, but
    observed fills/owned SL receipts must still be retained for close/recovery.
    A generation change for another bot is safe only after the same ownership,
    actual position and latest close-only state have all been checked again.
    """
    _current(lab, state, bot)
    current = _token(lab, bot)
    if current["owner"] != token["owner"] or current["position"] != token["position"]:
        raise PreflightError("protection_position_or_owner_changed")
    if current["generation"] != token["generation"]:
        token["generation"] = current["generation"]
    if _close_only(api, state, bot):
        bot["protection"] = {**bot.get("protection", {}),
                             "status": "RECOVERY_CLOSE_ONLY", "verified": False}
        return False
    return True


def _observe(lab, state, bot, api, stored):
    with _lock(lab):
        _current(lab, state, bot, stored)
        before = deepcopy(stored)
        observed = deepcopy(stored)
    try:
        row, proof = observe(api, observed)  # network; never owns the state lock
    except PreflightError as error:
        if before.get("receipt") is None and not before.get("terminal") and absent_receipt(error):
            return _never_shown(lab, state, bot, api, stored, before)  # R2.1.15 (QR-01; RC2 PP-03: a positive ACK waits too)
        raise
    with _lock(lab):
        _current(lab, state, bot, stored)
        if stored != before:
            raise PreflightError("protection_record_changed_during_io")
        stored.clear()
        stored.update(observed)
        _journal(lab, state, bot, before, stored, row)
        _note_match(lab, state, bot, stored)
    return row, proof


def _never_shown(lab, state, bot, api, stored, before):
    """R2.1.15 (QR-01, D187; RC2): 51603 on the GET of an owned stop OKX never showed (no receipt yet: its POST lost on the
    way, refused under a code not listed, or never sent after the record was journalled; or its GET lagging after a
    positive ACK). Every such read also reads the pending algo orders:
    - listed under our client ID: ours, its algoId joins the owned ones (the pending check of the same account check then
      knows it, PP-03), no verdict while it is listed: ProtectionAbsencePending;
    - not listed and not due (ABSENT_READS reads over ABSENT_SECONDS by clock()): protective_absent (its first read) and
      ProtectionAbsencePending;
    - due after a positive ACK (an algoId we were given): never "not created" (PP-03), the recovery: the close;
    - due otherwise: not created (protective_not_created), the record terminal; ensure then places a new stop in the same
      call (PP-02), one per position (PP-01), which OKX refuses for a price beyond the trigger (51278 / 51280: the close);
      a second verdict for the same position: the recovery and no more entries.
    A stop OKX showed once never comes here: 51603 on it is QA-03 (exchange_lab.stop_receipt_unseen)."""
    now = lab.clock()
    with _lock(lab):
        _current(lab, state, bot, stored)
        if stored != before:
            raise PreflightError("protection_record_changed_during_io")
    listed = api.get(STOP_PENDING, {"ordType": "conditional", "limit": "100"}, private=True)
    mine = next((r for r in listed if isinstance(r, dict) and r.get("algoClOrdId") == before["body"]["algoClOrdId"]), None)
    with _lock(lab):
        _current(lab, state, bot, stored)
        absent = stored.setdefault("absent", {"first_at": now, "reads": 0})
        absent["reads"] += 1
        seconds = round(now - absent["first_at"], 3)
        if absent["reads"] == 1:
            emit(state, now, bot, 4, "protective_absent",
                 "OKX не показує власного stop, якого ще жодного разу не показував: перечитуємо, без входів і докупівель.",
                 absent_reads=1, absent_seconds=0, **({"listed": True} if mine is not None else {}))
        bot["protection"] = {"status": "ABSENCE_PENDING", "verified": False}
        acked = next((a for a, c in api.owned_algos.items() if c == before["body"]["algoClOrdId"]), None)
        if acked and not stored.get("ack_algo_id"):
            stored["ack_algo_id"] = acked  # PP-03: the algoId of a positive ACK, kept with the record from its first absence
        algo_id = (mine or {}).get("algoId")
        if mine is not None and isinstance(algo_id, str) and algo_id.isdigit():
            stored["ack_algo_id"] = algo_id
            api.owned_algos[algo_id] = before["body"]["algoClOrdId"]
        due = absent["reads"] >= ABSENT_READS and seconds >= ABSENT_SECONDS
        if mine is not None or not due:
            lab.save(state)
            raise ProtectionAbsencePending("protective_absence_pending")
        if stored.get("ack_algo_id"):  # a positive ACK, not listed, not readable: no verdict, the recovery (the close)
            request_recovery(lab, state, bot)
            raise ProtectionAbsencePending("protective_absence_pending")
        proof = {"state": "not_created", "live_verified": False, "child_order_ids": [], "algo_id": None}
        stored.update(terminal=True, not_created=True, proof=proof, ack_uncertain=False)
        bot["_stops_not_created"] = bot.get("_stops_not_created", 0) + 1
        emit(state, lab.clock(), bot, 4, "protective_not_created",
             "Власного stop біржа так і не показала: запис закрито; ставимо новий stop або закриваємо позицію."
             if bot["_stops_not_created"] <= REPLACEMENT_STOPS else
             "Власного stop біржа знову не створила: нових входів цього бота до кінця раунду немає, позиція закривається.",
             absent_reads=absent["reads"], absent_seconds=seconds, stops_not_created=bot["_stops_not_created"])
        if bot["_stops_not_created"] > REPLACEMENT_STOPS:  # RC2 (PP-01): a lasting failure, not one more replacement
            bot["entries_blocked"] = "protection_not_created"
            request_recovery(lab, state, bot)
        lab.save(state)
    return None, proof


def stop_matches(state, bot, stored):
    """R2.1.10 (A3): the owned stop's last GET receipt is live, for the actual size, with a trigger no wider than the
    recipe's stop from the entry price, and no amendment is pending."""
    proof, body = stored.get("proof") or {}, stored["body"]
    try:
        contracts, long = abs(Decimal(bot["_contracts"])), Decimal(bot["_contracts"]) > 0
        recipe = next(r for r in state["strategies"] if r["id"] == bot["id"])
        stop = Decimal(str(recipe["rules"]["stop_loss_bps"]))
        raw = Decimal(str(bot["entry_price"])) * (1 - stop / 10000 if long else 1 + stop / 10000)
        trigger = Decimal(body["slTriggerPx"])
        return (proof.get("live_verified") is True and proof.get("state") == "live" and contracts > 0 and not stored.get("pending_amend")
                and not stored.get("terminal") and Decimal(body["sz"]) == contracts and (trigger >= raw if long else trigger <= raw))
    except (ArithmeticError, KeyError, StopIteration, TypeError, ValueError):
        return False


def _note_match(lab, state, bot, stored):
    """R2.1.10 (A3): the monotonic time of the last matching GET receipt (stop_matches), per process: updated on every
    match, cleared on any other receipt."""
    resource = (getattr(lab, "resources", None) or {}).get(state["id"])
    if resource is None or not callable(getattr(lab, "monotonic", None)):
        return  # no running worker: nothing to extend
    matched = resource.setdefault("stop_matched_mono", {})
    if stop_matches(state, bot, stored):
        matched[bot["id"]] = lab.monotonic()
    else:
        matched.pop(bot["id"], None)


def _journal(lab, state, bot, before, stored, row):
    """The owned stop's life cycle in the protected journal, with the venue's own facts: placed, amended, terminal."""
    was, now = before.get("proof") or {}, stored.get("proof") or {}
    body = stored["body"]
    facts = {"algo_id": now.get("algo_id"), "algo_client_id": body["algoClOrdId"], "contracts": body["sz"],
             "trigger_price": body["slTriggerPx"], "trigger_type": body.get("slTriggerPxType"), "venue_state": now.get("state"),
             "venue_updated_ms": (row or {}).get("uTime"), "venue_created_ms": (row or {}).get("cTime")}
    if now.get("live_verified") and not was.get("live_verified"):
        emit(state, lab.clock(), bot, 4, "protective_placed", "Власний stop стоїть на біржі: GET-квитанцію перевірено.",
             post_seconds=stored.get("post_seconds"), **facts)
    elif now.get("live_verified") and before.get("body") != body:
        emit(state, lab.clock(), bot, 4, "protective_amended", "Розмір або тригер власного stop змінено й підтверджено GET.",
             previous_contracts=before["body"]["sz"], previous_trigger_price=before["body"]["slTriggerPx"],
             post_seconds=stored.get("amend_post_seconds"), **facts)
    if now.get("state") != was.get("state") and now.get("state") in {"canceled", "order_failed", "effective"}:  # final OKX algo states
        source = {}
        if now.get("state") == "canceled":  # our flat-position cleanup, or the venue itself (cxlOnClosePos after a close)
            source = {"cancelled_by": "own_cleanup" if stored.get("cancel_started") else "venue",
                      **({"venue_cancel_source": row["cancelSource"]} if (row or {}).get("cancelSource") else {})}
        if (row or {}).get("cancelSource") is not None:  # R2.1.10 (C): the venue's code under a new, exported key
            source["cancel_source_code"] = str(row["cancelSource"])
        emit(state, lab.clock(), bot, 6, "protective_terminal", "Власний stop завершено на біржі.", **facts, **source)


def request_recovery(lab, state, bot):
    """Latch close-only; ownership must still pass checked_account before a close.

    An unavailable receipt is UNKNOWN, never evidence that the POST was rejected.
    Persisted intents remain available for reconciliation; no replacement POST.
    """
    with _lock(lab):
        _current(lab, state, bot)
        if not bot.get("_protection_recovery"):
            bot["_protection_recovery"] = {"policy": RECOVERY_POLICY, "at": lab.clock()}
            bot["protection"] = {**bot.get("protection", {}), "verified": False,
                                 "status": "RECOVERY_REQUIRED"}
            state["incomplete"] = True
            emit(state, lab.clock(), bot, 6, "protective_recovery",
                 "Захист не підтверджено: лише звірка й закриття власного залишку; без нового входу чи Jev.",
                 policy=RECOVERY_POLICY)
        bot.pop("_reduce_remaining", None)
        engine.stop(state, lab.clock(), bot["id"], "Захист позиції не підтверджено.", code="protection_unverified")
        lab.save(state)


def intent_of(row):
    body = row["body"]
    return ProtectionIntent(body["algoClOrdId"], body, hashlib.sha256(canonical(body)).hexdigest(), body["sz"])


def result_for(api, bot, order):
    parent = order.get("_protective_parent")
    if parent:
        stored = next(p for p in bot.get("_protections", []) if p["body"]["algoClOrdId"] == parent)
        _, result = api.protective_child(intent_of(stored), stored["receipt"], order["_child_id"],
                bot["_contracts"], (order.get("result") or {}).get("contracts", "0"))
        return result
    return api.order_result(order["request"])


def observe(api, stored):
    pending = stored.get("pending_amend")
    alternative = intent_of({"body": pending}) if pending else None
    row, proof = api.protective_details(intent_of(stored),
        known_algo_id=(stored.get("proof") or {}).get("algo_id"), alternate=alternative)
    if pending:
        try:
            validate_stop_receipt(alternative, row, known_algo_id=proof["algo_id"])
        except ValueError:
            pass  # Old protection remains confirmed; uncertain amendment is not retried.
        else:
            stored["body"] = deepcopy(pending)
            stored.pop("pending_amend", None)
            stored["amend_uncertain"] = False
    stored.update(receipt=deepcopy(row), proof=proof)
    if proof["live_verified"]:
        stored["ack_uncertain"] = False
    return row, proof


def terminal_proof(bot, stored, proof):
    """Flat inventory and a cancel ACK are not evidence about unresolved children."""
    children = [o for o in bot["_orders"] if o.get("_protective_parent") == stored["body"]["algoClOrdId"]]
    terminal_parent = (proof["state"] in {"canceled", "order_failed"}
                       or proof["state"] == "effective" and bool(proof["child_order_ids"]))
    return (terminal_parent and set(proof["child_order_ids"]).issubset({o["_child_id"] for o in children})
            and all((o.get("result") or {}).get("state") in {"filled", "canceled"} for o in children))


def reconcile(lab, state, bot, api):
    """Adopt only children tied to persisted owned parent BEFORE position comparison."""
    with _lock(lab):
        _current(lab, state, bot)
        records = list(bot.get("_protections", []))
    for stored in records:
        with _lock(lab):
            _current(lab, state, bot, stored)
            if stored.get("terminal"):
                continue
        try:
            row, proof = _observe(lab, state, bot, api, stored)
        except ProtectionAbsencePending:
            continue  # R2.1.15 (QR-01): the check goes on; nothing of this record to adopt
        deferred = False
        with _lock(lab):
            if proof["state"] in {"canceled", "order_failed"} and Decimal(bot["_contracts"]):
                if closing_in_flight(bot):
                    # R219-CLOSE-LATE: OKX takes the stop off at our own close (cxlOnClosePos, ~20 ms) and the close's facts may
                    # show a read later; re-evaluated once they are read (the unseen window bounds the wait). A close that
                    # resolved with contracts left gets the recovery, as before.
                    deferred = True
                    if not stored.get("close_pending_noted"):
                        stored["close_pending_noted"] = True
                        emit(state, lab.clock(), bot, 6, "protective_close_pending",
                             "Стоп знято, поки факти нашого закриття ще не прочитані: оцінюємо після них.")
                else:
                    request_recovery(lab, state, bot)
        for child in proof["child_order_ids"]:
            with _lock(lab):
                _current(lab, state, bot, stored)
                order = next((o for o in bot["_orders"] if o.get("_child_id") == child), None)
                contracts = bot["_contracts"]
                previous = deepcopy(order.get("result")) if order else None
                intent = intent_of(stored)
            body, result = api.protective_child(intent, row, child, contracts,
                (previous or {}).get("contracts", "0"))
            with _lock(lab):
                _current(lab, state, bot, stored)
                current = next((o for o in bot["_orders"] if o.get("_child_id") == child), None)
                if bot["_contracts"] != contracts or current is not order or (order and order.get("result") != previous):
                    raise PreflightError("protective_child_state_changed_during_io")
                if order is None:
                    order = {"request": body, "at": lab.clock(), "send_started": False, "result": None,
                             "_protective_parent": stored["body"]["algoClOrdId"], "_child_id": child}
                    bot["_orders"].append(order)
                    emit(state, lab.clock(), bot, 6, "protective_child_adopted", "Підтверджено власний біржовий stop-child; враховуємо cumulative fill.")
                engine.record_result(state, bot, order, result, lab.clock(), seen_by="protection")
        with _lock(lab):
            _current(lab, state, bot, stored)
            if terminal_proof(bot, stored, proof) and not deferred:
                stored["terminal"] = True
            lab.save(state)


def fired_child_unread(bot, stored):
    """R2111-RECHECK-FIRED: the stop fired (effective or partially_effective) and a child it names is not read yet or not
    resolved: the record is on its way to terminal, not a mismatch (the reviewer's variant B)."""
    proof = stored.get("proof") or {}
    if proof.get("state") not in {"effective", "partially_effective"}:
        return False
    children = {o["_child_id"]: o for o in bot["_orders"]
                if o.get("_protective_parent") == stored["body"]["algoClOrdId"] and o.get("_child_id")}
    return any(c not in children or (children[c].get("result") or {}).get("state") not in {"filled", "canceled", "mmp_canceled"}
               for c in proof.get("child_order_ids") or ())  # no child listed: nothing on its way, so a mismatch


def closing_in_flight(bot):
    """R219-CLOSE-LATE: our own reduce-only close (not a stop's child) whose facts are not read yet."""
    return any(o["request"]["reduceOnly"] and not o.get("_protective_parent") for o in engine.unresolved(bot))


def own_stop_fired(bot, error):
    """R2.1.14 (D164): _ensure saw a child of our own stop and no order of ours but the stop's children is open: our own stop
    fired, nothing else; the check then reads the child and compares the position as for the first window (a size that does
    not agree is the manual review there). An open order of ours keeps the recovery, as before."""
    return (str(error) == "protection_triggered_reconcile_first"
            and not any(not o.get("_protective_parent") for o in engine.unresolved(bot)))


STOP_RECEIPT = "/api/v5/trade/order-algo"


def read_failure(error):
    """R2.1.15 (D183 p. 1, QA-02): the GET of an owned stop's receipt failed at the venue or on the way (request() names the
    endpoint and method of every failure it raises); a receipt read but not ours (protective_receipt_mismatch) is no such."""
    return getattr(error, "method", None) == "GET" and getattr(error, "endpoint", None) == STOP_RECEIPT


def ensure(lab, state, bot, api, *, own_fire=False):
    """own_fire (R2.1.14, D164): the account check's call, after its position GET. Our own stop fired since that GET (the
    second window of the race): no recovery, PreflightError("protective_fired_after_position_read") instead, and the check
    reads the stop's child as it does for the first window.

    R2.1.15 (D183 p. 1, QA-02): in that call, with the stop just read live and matching in the same check (reconcile;
    stop_matches) and nothing placed or amended since, a failed GET of its receipt is an ordinary failure of the check (A1,
    A3, three in a row), as the same failure of the first read is. The recovery stays for a placement, an amendment, a
    receipt that differs and a child."""
    with _lock(lab):
        if Decimal(bot["_contracts"]) and closing_in_flight(bot):
            return  # R219-CLOSE-LATE: no stop placed or amended while our own close is unread; the next check decides
        records = bot.get("_protections") or []
        live = next((p for p in reversed(records) if not p.get("terminal")), None)
        matched, count = own_fire and live is not None and stop_matches(state, bot, live), len(records)
    try:
        return _ensure(lab, state, bot, api)
    except ProtectionAbsencePending:
        return  # R2.1.15 (QR-01): the verdict on a stop OKX never showed is not due yet; no recovery, no failed check
    except (PreflightError, OSError, ValueError, KeyError, TypeError) as error:
        with _lock(lab):
            _current(lab, state, bot)
            if own_fire and own_stop_fired(bot, error):
                raise PreflightError("protective_fired_after_position_read") from error
            unchanged = (matched and read_failure(error) and len(bot.get("_protections") or []) == count
                         and not live.get("pending_amend") and not live.get("terminal"))
            if Decimal(bot["_contracts"]) and not unchanged:
                request_recovery(lab, state, bot)
        raise


def _ensure(lab, state, bot, api):
    """No replacement POST after an uncertain ACK. Amend keeps existing SL armed."""
    with _lock(lab):
        _current(lab, state, bot)
        token = _token(lab, bot)
        quantity = Decimal(bot["_contracts"])
        records = bot.setdefault("_protections", [])
        live = next((p for p in reversed(records) if not p.get("terminal")), None)
    if not quantity:
        with _lock(lab):
            bot["protection"] = {"status": "CLEANUP_PENDING", "verified": False}
            cleanup = list(records)
        for row in cleanup:
            with _lock(lab):
                if row.get("terminal"): continue
            _, proof = _observe(lab, state, bot, api, row)
            with _lock(lab):
                _current(lab, state, bot, row)
                if Decimal(bot["_contracts"]):
                    raise PreflightError("protection_position_changed_during_cleanup")
                cancel = proof["live_verified"] and not row.get("cancel_started")
                if cancel:
                    row["cancel_started"] = True; lab.save(state)
                elif terminal_proof(bot, row, proof):
                    row["terminal"] = True
            if cancel:
                sent, outcome = lab.clock(), "uncertain"
                try:
                    api.cancel_protection(proof["algo_id"])
                    outcome = "acknowledged"
                finally:  # our own cancel of the stop is journalled, as every order POST is, whatever its answer
                    with _lock(lab):
                        emit(state, lab.clock(), bot, 6, "protective_cancel_posted", "POST зняття власного stop: позиція вже нульова.",
                             algo_id=proof["algo_id"], outcome=outcome, post_seconds=round(lab.clock() - sent, 3))
            # Flat inventory does not settle an effective parent whose child
            # receipt is still missing (Stop/SL race).
        with _lock(lab):
            _current(lab, state, bot)
            if all(row.get("terminal") for row in records):
                bot["protection"] = {"status": "FLAT", "verified": True}
                bot.pop("_stops_not_created", None)  # RC2 (PP-01): one replacement per position
            lab.save(state)
        return
    with _lock(lab):
        if not _recheck(lab, state, bot, api, token):
            return
        recipe = next(r for r in state["strategies"] if r["id"] == bot["id"])
        index = state["bots"].index(bot)
        identity = live["body"]["algoClOrdId"] if live else "J8" + state["id"][:16] + str(index) + "S" + str(len(records)).zfill(4)
        desired = owned_stop_intent(client_id=identity, contracts=quantity, basis=bot["entry_price"],
                     stop_bps=recipe["rules"]["stop_loss_bps"], tick_size=bot["_instrument"]["tick_size"],
                     instrument=bot["_instrument"]["instrument"])
        placing = live is None
        if placing:
            if len(records) >= 40:
                raise PreflightError("protection_unavailable_blocks_admission")
            live = {"body": deepcopy(desired.body), "at": lab.clock(), "send_started": True, "receipt": None}
            records.append(live)
            bot["protection"] = {"status": "PLACEMENT_PENDING", "verified": False}
            emit(state, lab.clock(), bot, 4, "protective_intent", "Зарезервовано власний reduce-only stop на фактичний обсяг.")
            # Durable send reservation is the linearization point. Stop may now
            # race this in-flight reduce-only POST; its receipt is reconciled,
            # never replaced blindly. New entry/add remains submit()-guarded.
            lab.save(state)
    if placing:
        sent = lab.clock()
        try:
            api.place_protection(desired)  # RC2 (PP-03): a positive ACK's algoId joins the client's owned algos
        except ProtectionRejected as error:
            # Preserve the actual negative ACK separately; never invent a GET
            # receipt or algo ID for a stop the provider explicitly rejected.
            with _lock(lab):
                _current(lab, state, bot, live)
                live.update(terminal=True, rejected_code=error.code, ack_uncertain=False)
                emit(state, lab.clock(), bot, 6, "protective_placement_rejected",
                     "Біржа відхилила створення захисту; звіряємо й закриваємо власний залишок.", provider_code=error.code)
            raise
        except (PreflightError, OSError, ValueError):
            with _lock(lab):
                _current(lab, state, bot, live)
                live["ack_uncertain"] = True
        with _lock(lab):
            _current(lab, state, bot, live)
            live["post_seconds"] = round(lab.clock() - sent, 3)  # POST send to its answer (or failure)
    _, proof = _observe(lab, state, bot, api, live)
    if proof.get("state") == "not_created":
        return _ensure(lab, state, bot, api)  # RC2 (PP-02): the record is closed; the new stop now (close-only: none)
    with _lock(lab):
        if proof["child_order_ids"]:
            raise PreflightError("protection_triggered_reconcile_first")
        if not proof["live_verified"]:
            bot["protection"] = {"status": "UNVERIFIED", "verified": False}
            raise PreflightError("protection_unverified")
        if not _recheck(lab, state, bot, api, token):
            lab.save(state)
            return
        if live.get("pending_amend"):
            bot["protection"] = {"status": "AMEND_PENDING", "verified": False}
            raise PreflightError("protective_amend_uncertain")
        old = live["body"]
        price = max if quantity > 0 else min  # never widen the armed threshold
        desired.body["slTriggerPx"] = str(price(Decimal(old["slTriggerPx"]), Decimal(desired.body["slTriggerPx"])))
        amending = Decimal(old["sz"]) != abs(quantity) or Decimal(old["slTriggerPx"]) != Decimal(desired.body["slTriggerPx"])
        if amending:
            if live.get("amend_uncertain"):
                raise PreflightError("protective_amend_uncertain")
            live["pending_amend"] = deepcopy(desired.body); live["amend_uncertain"] = True
            bot["protection"] = {"status": "AMEND_PENDING", "verified": False}
            amend_intent = intent_of({"body": live["pending_amend"]})
            bot["_amend_seq"] = bot.get("_amend_seq", 0) + 1  # its own id kind: never the clOrdId of a later close order
            amend_id = "J5" + state["id"][:16] + str(state["bots"].index(bot)) + "A" + str(bot["_amend_seq"]).zfill(4)
            lab.save(state)
    if amending:
        sent = lab.clock()
        try:
            api.amend_protection(proof["algo_id"], amend_intent, amend_id)
        except (PreflightError, OSError, ValueError):
            pass  # UNKNOWN ACK; GET may prove the exact amendment was accepted.
        with _lock(lab):
            _current(lab, state, bot, live)
            live["amend_post_seconds"] = round(lab.clock() - sent, 3)
        _, proof = _observe(lab, state, bot, api, live)
    with _lock(lab):
        if not _recheck(lab, state, bot, api, token):
            lab.save(state)
            return
        if live.get("pending_amend"):
            raise PreflightError("protective_amend_uncertain")
        bot["protection"] = {"status": "ARMED", "verified": proof["live_verified"], "covered_contracts": desired.body["sz"],
                             "trigger_price": desired.body["slTriggerPx"], "verified_at": lab.clock()}
        lab.save(state)
