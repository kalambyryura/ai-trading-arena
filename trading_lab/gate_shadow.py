"""Read-only analysis of retained gate bytes. No model/exchange transport or execution."""
from collections import defaultdict
from decimal import Decimal
import math
import random
import statistics

from .arena_strategies import ArenaError
from .battle_review import number, request_evidence, verify_bundle


def examples_in(bundle, strategies):
    """R2111-GATE: strategies.json of the bundle's own Battle ZIP, checked against it (the same participants, each
    recipe hashing to the bundle's recipe_sha256); the ids written by an example author (D135 p. 7)."""
    from .arena_strategies import fingerprint
    expected = {p["id"]: p["recipe_sha256"] for p in bundle["participants"] if p["id"] != "cash"}
    rows = {r.get("id"): r for r in strategies if isinstance(r, dict)} if isinstance(strategies, list) else {}
    if (len(rows) != len(strategies or []) or set(rows) != set(expected)
            or any(rows[i].get("recipe_sha256") != h or not isinstance(rows[i].get("recipe"), dict)
                   or fingerprint(rows[i]["recipe"]) != h for i, h in expected.items())):
        raise ArenaError("strategies.json не з цього бою: учасники або recipe_sha256 не збігаються з bundle.")
    return frozenset(i for i, r in rows.items() if (r["recipe"].get("author") or {}).get("kind") == "example")


def cases(bundle, repeat=1, examples=frozenset()):
    """examples (R2.1.11, E): participant ids of an example author (from strategies.json or the journal), never a case."""
    verify_bundle(bundle)
    if bundle.get("match", {}).get("execution_mode") != "rules_only" or type(repeat) is not int or not 1 <= repeat <= 20:
        raise ArenaError("Shadow потребує RULES_ONLY bundle; repeat має бути 1..20.")
    participants = {p["id"]: p for p in bundle["participants"]}
    events = bundle["events"]
    by_seq = {e["seq"]: e for e in events}
    result = []
    for request in bundle.get("request_payloads", []):
        event = by_seq[request["seq"]]
        if event["code"] != "jev_shadow_payload" or request.get("sent") is not False:
            raise ArenaError("Shadow payload не є ненадісланою подією.")
        proof = request_evidence(request["payload"], request, unsent_shadow=True)
        if proof["wire_hash_status"] != "VERIFIED_WIRE" or proof["payload"]["state"]["mode"] != bundle["match"]["mode"]:
            raise ArenaError("Shadow потребує точних wire bytes та незмінного mode.")
        wire = proof["payload_wire_json"].encode()
        ids = request.get("candidate_ids", [])
        slots = {}
        if (len(ids) != len(proof["payload"]["questions"]) or len(set(ids)) != len(ids)
                or any(identity not in participants for identity in ids)):
            raise ArenaError("Shadow candidate_ids не відповідають учасникам.")
        for index, identity in enumerate(ids):
            if identity in examples:
                continue  # D135 p. 7: the technical probe and samples are no gate evidence
            slot = "c" + str(index)
            candidate = proof["payload"]["state"][slot]
            participant = participants[identity]
            if candidate["recipe_sha256"] != participant["recipe_sha256"]:
                raise ArenaError("Shadow candidate не відповідає рецепту.")
            # Only a causal entry_rechecked link can attribute an observed round trip.
            ledger = participant["trade_ledger"]
            all_costs = Decimal(0)
            cost_evidence_complete = True
            for row in ledger["rows"]:
                for field in (("signed_funding_usdt",) if row.get("role") == "funding" else ("gross_pnl_usdt", "signed_fees_usdt")):
                    value = number(row.get(field))
                    if value is None: cost_evidence_complete = False
                    else: all_costs += Decimal(value)
            final_net = number(participant["costs"].get("net_usdt"))
            cost_evidence_complete = cost_evidence_complete and final_net is not None and abs(all_costs-Decimal(final_net)) <= Decimal(".00000001")
            linked = []
            for trip in ledger["round_trips"]:
                if candidate["kind"] != "entry" or not trip.get("closed"):
                    continue
                checks = [e for e in events if e["bot_id"] == identity and e["code"] == "entry_rechecked"
                          and e["seq"] < trip["entry_seq"]]
                prior = checks[-1] if checks else None
                if not prior or number(prior["data"].get("shadow_event_seq")) != str(request["seq"]):
                    continue
                rows = [r for r in ledger["rows"] if r.get("seq") in trip["fill_seqs"]]
                # Funding observed within this round-trip boundary, not allocated from final totals.
                rows += [r for r in ledger["rows"] if r.get("role") == "funding"
                         and trip["entry_seq"] <= r.get("seq", -1) <= trip["close_seq"]]
                complete = cost_evidence_complete and participant["quality"]["competitive_validity"]["status"] == "ELIGIBLE"
                # A late aggregated funding observation cannot be attributed to an earlier cycle.
                if any(r.get("role") == "funding" and r.get("seq", -1) > trip["close_seq"] for r in ledger["rows"]):
                    complete = False
                total = Decimal(0)
                for row in rows:
                    for field in (("signed_funding_usdt",) if row.get("role") == "funding" else ("gross_pnl_usdt", "signed_fees_usdt")):
                        value = number(row.get(field))
                        if value is None:
                            complete = False
                        else:
                            total += Decimal(value)
                # Add/reduce within a trip are already included; never count their decision a second time.
                linked.append({"entry_seq": trip["entry_seq"], "close_seq": trip["close_seq"],
                               "observed_net_usdt": str(total) if complete else None,
                               "complete": complete})
            slots[slot] = {"bot_id": identity, "recipe_sha256": participant["recipe_sha256"],
                           "kind": candidate["kind"], "rules_allowed": candidate["local_checks_passed"] is True,
                           "linked_trips": linked}
        if not slots:
            continue  # R2111-GATE: every candidate an example author: no case, so nothing of it reaches Jev
        for r in range(repeat):
            result.append({"case_id": "shadow:seq%d:r%d" % (request["seq"], r), "body": wire,
                           "meta": {"group": "recorded_shadow", "seq": request["seq"], "repeat": r,
                                    "bytes": len(wire), "sendable": len(wire) <= 8000,
                                    "local_checks_passed": all(s["rules_allowed"] for s in slots.values()),
                                    "slots": slots}})
    if not result:
        raise ArenaError("Bundle не містить точних ненадісланих Shadow payload учасників, що рахуються "
                         "(бій лише з навчальними прикладами не є доказом гейту).")
    return result


def comparison(records):
    """Descriptive observed subsets. A veto changes later cycles; this is not counterfactual PnL."""
    decisions, trips, repeats = [], {}, defaultdict(list)
    for record in records:
        meta = record.get("meta") or {}
        for slot, facts in meta.get("slots", {}).items():
            answer = (record.get("answers") or {}).get(slot)
            confidence = answer.get("confidence") if isinstance(answer, dict) else None
            valid = (record.get("status") == "ok" and isinstance(answer, dict) and answer.get("type") == "choice"
                     and answer.get("choice") in {"proceed", "hold"} and type(confidence) in {int, float}
                     and math.isfinite(confidence) and 0 <= confidence <= 1)
            passed = answer["choice"] == "proceed" and confidence >= .70 if valid else None
            decisions.append(passed)
            if passed is not None:
                repeats[(meta["seq"], slot)].append(passed)
            if meta.get("repeat", 0) == 0 and passed is not None:
                for trip in facts.get("linked_trips", []):
                    if trip.get("complete") and trip.get("observed_net_usdt") is not None:
                        trips[(facts["bot_id"], trip["entry_seq"])] = {**trip, "passed": passed}
    sample = list(trips.values())
    accepted = [float(t["observed_net_usdt"]) for t in sample if t["passed"]]
    blocked = [float(t["observed_net_usdt"]) for t in sample if not t["passed"]]
    valid = [v for v in decisions if v is not None]
    statistic = None
    if len(accepted) >= 2 and len(blocked) >= 2:
        variance = statistics.variance(accepted)/len(accepted) + statistics.variance(blocked)/len(blocked)
        if variance > 0:
            statistic = (statistics.mean(accepted)-statistics.mean(blocked))/math.sqrt(variance)
    placebo = None
    if accepted and blocked:
        population = [float(t["observed_net_usdt"]) for t in sample]
        rng = random.Random(0)
        sums = sorted(sum(rng.sample(population, len(accepted))) for _ in range(1000))
        placebo = {"draws": 1000, "same_accept_count": len(accepted), "net_sum_p05": sums[49], "net_sum_p95": sums[949],
                   "fraction_ge_observed": sum(v >= sum(accepted) for v in sums)/len(sums)}
    return {"schema": "recorded-gate-shadow-comparison/v1", "decisions": len(decisions), "valid_answers": len(valid),
            "pass_ge_0_70": sum(valid)/len(valid) if valid else None,
            "repeat_disagreements": sum(len(set(values)) > 1 for values in repeats.values()),
            "linked_closed_trips": len(sample), "accepted_net_usdt": sum(accepted), "blocked_net_usdt": sum(blocked),
            "welch_t_descriptive": statistic, "placebo": placebo,
            "limitations": ["Observed trade subsets, not simulated alternative equity: rejected entries change later cycles.",
                             "No profitability or predictive edge claim. Add decisions are not counted as a second trip.",
                             "Incomplete/unsettled trips excluded; funding only from recorded in-trip observations."]}
