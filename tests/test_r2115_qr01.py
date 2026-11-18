"""R2.1.15 (D187; review team/reviews/LIVE_MIX_47BC7D1E_REVIEW_UA.md, QR-01 STOP-NEVER-CREATED; mirrors of the reviewer's
probes p03-p05). An owned stop OKX never created no longer blocks the bot for good: a POST lost on the way, a refusal
51280 / 51278 (the price already beyond the trigger), a journal fault on the stop's intent. And the boundary with QA-03:
51603 on a stop OKX showed once stays "not known yet" inside the STOP-FIRED-LAG wait. Strict fakes, temp roots: no
network, key, Jev or order."""
from decimal import Decimal
import json
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
import unittest

import test_order_outcomes as outcomes
from trading_lab import exchange_protection as protection, ops_storage
from trading_lab.okx_readonly import PreflightError
from trading_lab.ops_storage import StorageBlocked

ALGO = "/api/v5/trade/order-algo"


class StopNeverCreated(outcomes.SeasonCase):
    TAKER, DURATION, SIDE = True, 3600, "long"

    def setUp(self):
        super().setUp()
        self.lose, self.refuse, self.form, self.lose_ack, self.lag, self.created = 0, None, "row", 0, 0, {}
        self.vanish = 0
        api = self.fx.lab.resources[self.state["id"]]["apis"][0]
        self.wrap(api)

    def wrap(self, api):
        inner = api.transport

        def transport(request):
            route, method = urlsplit(request.full_url), request.get_method()
            if route.path == ALGO and method == "POST" and self.lose:
                self.lose -= 1
                raise PreflightError("network_error")  # a connect error: OKX never saw this POST
            if route.path == ALGO and method == "POST" and self.refuse:
                body = json.loads(request.data)
                self.fake.requests.append(request)
                if self.form == "top":  # the refusal as a top-level code with no rows
                    return {"code": self.refuse, "msg": "", "data": []}
                echoed = "" if self.form == "no_id" else body["algoClOrdId"]
                return {"code": "1", "msg": "", "data": [{"sCode": self.refuse, "sMsg": "", "algoClOrdId": echoed, "algoId": ""}]}
            if route.path == ALGO and method == "POST" and self.lose_ack:
                self.lose_ack -= 1
                inner(request)  # OKX creates the stop; its answer is lost on the way back
                self.created[json.loads(request.data)["algoClOrdId"]] = self.fx.now
                raise PreflightError("network_error")
            if route.path == ALGO and method == "POST":
                answer = inner(request)
                client = json.loads(request.data)["algoClOrdId"]
                self.created[client] = self.fx.now
                if self.vanish:  # a positive ACK, and then OKX has no such stop: not listed, its GET 51603
                    self.vanish -= 1
                    self.fake.algos.pop(client)
                return answer
            if route.path == ALGO and method == "GET":
                client = parse_qs(route.query)["algoClOrdId"][0]
                if client not in self.fake.algos or self.fx.now < self.created.get(client, -1) + self.lag:
                    self.fake.requests.append(request)
                    return {"code": "51603", "msg": "Order does not exist", "data": []}  # as OKX, for a stop it does not show
            return inner(request)
        api.transport = transport

    def posts(self, path=ALGO):
        return [json.loads(r.data) for r in self.fake.requests if r.get_method() == "POST" and urlsplit(r.full_url).path == path]

    def live_algos(self):
        return [a for a in self.fake.algos.values() if a["state"] == "live"]

    def test_p04_a_lost_post_ends_in_not_created_and_a_new_stop(self):
        self.lose = 1
        self.run_season(until=lambda: bool(self.events("protective_not_created")) and bool(self.live_algos())
                        and (self.bot.get("protection") or {}).get("status") == "ARMED", limit=60)
        absent, verdict = self.events("protective_absent"), self.events("protective_not_created")
        self.assertEqual(len(absent), 1)
        self.assertEqual(len(verdict), 1)
        self.assertGreaterEqual(verdict[0]["data"]["absent_reads"], protection.ABSENT_READS)
        self.assertGreaterEqual(verdict[0]["data"]["absent_seconds"], protection.ABSENT_SECONDS)
        self.assertFalse(self.events("exchange_check_failed"))  # the wait for the verdict fails no check
        self.assertFalse(self.events("protective_recovery"))
        self.assertFalse(self.bot["_manual_review"])
        self.assertFalse(self.bot["stop_requested"])
        records = self.bot["_protections"]
        self.assertTrue(records[0]["not_created"] and records[0]["terminal"])
        self.assertEqual(len(self.live_algos()), 1)  # the new stop, for the whole position
        self.assertEqual(Decimal(self.live_algos()[0]["sz"]), abs(Decimal(self.bot["_contracts"])))
        entries = [e for e in self.events("exchange_order_intent") if e["data"]["request"]["reduceOnly"] is False]
        self.assertEqual(len(entries), 1)  # no add while the stop was unknown
        self.fx.lab.stop(self.fx.lab.data["generation"], self.state["id"])  # the round ends cleanly with the new stop
        self.run_season(limit=40)
        self.assertEqual(self.state["status"], "DONE")
        self.assertEqual(self.fake.units, 0)
        self.assertTrue(self.bot["accounting_reconciled"])

    def test_p04_no_verdict_before_three_reads_and_60_s(self):
        self.lose = 1
        self.run_season(until=lambda: bool(self.events("protective_absent")), limit=20)
        first = self.fx.now
        self.run_season(until=lambda: bool(self.events("protective_not_created")), limit=40)
        self.assertGreaterEqual(self.fx.now - first, protection.ABSENT_SECONDS - 5)
        self.assertEqual((self.bot.get("protection") or {}).get("status") in {"ABSENCE_PENDING", "PLACEMENT_PENDING", "ARMED"}, True)

    def test_p04_the_local_stop_meanwhile_closes_reduce_only_without_waiting_for_the_verdict(self):
        """D187 (lead): the stop is never created, and the price reaches the recipe's local stop before the verdict: the
        local exit closes reduce-only at once; nothing waits for the verdict."""
        self.lose = 1
        self.run_season(until=lambda: bool(self.events("protective_absent")), limit=20)
        self.assertFalse(self.events("protective_not_created"))
        move = Decimal(".99") if self.SIDE == "long" else Decimal("1.01")
        self.fake.bid, self.fake.ask = outcomes.PRICE * move, outcomes.PRICE * move + Decimal(".01")  # 100 bps against: past SL 50
        began = self.fx.now
        self.run_season(until=lambda: self.fake.units == 0, limit=6)
        self.assertEqual(self.fake.units, 0)
        self.assertLess(self.fx.now - began, protection.ABSENT_SECONDS)  # before any verdict was due
        self.assertFalse(self.events("protective_not_created"))
        exits = self.events("exit_trigger")
        self.assertEqual(exits[-1]["data"]["exit_reason"], "stop_loss")
        closes = [p for p in self.posts("/api/v5/trade/order") if p["reduceOnly"]]
        self.assertEqual(len(closes), 1)
        self.assertFalse(self.bot["_manual_review"])
        self.assertFalse(self.events("protective_recovery"))

    def refused(self, code):
        self.refuse = code
        self.run_season(until=lambda: bool(self.events("protective_placement_rejected")), limit=40)
        self.refuse = None
        rejected = self.events("protective_placement_rejected")
        self.assertEqual(rejected[0]["data"]["provider_code"], code)
        self.assertEqual(len(self.events("protective_recovery")), 1)
        self.assertEqual(self.events("stop_requested")[-1]["data"]["reason_code"], "protection_unverified")
        self.run_season(limit=40)
        self.assertEqual(self.state["status"], "DONE")
        self.assertEqual(self.fake.units, 0)  # closed reduce-only at once
        closes = [p for p in self.posts("/api/v5/trade/order") if p["reduceOnly"]]
        self.assertTrue(closes)
        self.assertFalse(self.events("protective_absent"))  # an explicit refusal needs no verdict

    def test_p05_a_refusal_51280_closes_reduce_only(self):
        self.refused("51280")

    def test_pp01_a_refusal_without_our_client_id_closes_reduce_only(self):
        self.form = "no_id"
        self.refused("51280")

    def test_pp01_a_refusal_as_a_top_level_code_closes_reduce_only(self):
        self.form = "top"
        self.refused("51280")

    def test_pp01_a_lasting_unlisted_refusal_gets_one_replacement_then_the_recovery(self):
        """RC2 (PP-01, the reviewer's p07): every stop POST refused by a code we do not list (51088). One replacement for
        the position; the second verdict is the recovery: the close, and no entry for the rest of the round."""
        self.refuse = "51088"
        self.run_season(until=lambda: bool(self.events("protective_recovery")), limit=80)
        verdicts = self.events("protective_not_created")
        self.assertEqual([v["data"]["stops_not_created"] for v in verdicts], [1, 2])
        self.assertEqual(len(self.events("protective_recovery")), 1)
        self.assertEqual(self.bot["entries_blocked"], "protection_not_created")
        self.run_season(limit=60)
        self.assertEqual(self.fake.units, 0)
        entries = [e for e in self.events("exchange_order_intent") if e["data"]["request"]["reduceOnly"] is False]
        self.assertEqual(len(entries), 1)  # never in again without a stop
        self.assertEqual(len([p for p in self.posts() if p.get("algoClOrdId")]), 2)  # the first stop and its one replacement

    def test_pp02_a_verdict_on_the_second_read_places_the_new_stop_in_that_check(self):
        """RC2 (PP-02): the 60 s limit falls between the check's two reads of the stop (reconcile, then ensure): the verdict
        in ensure places the new stop in the same call, no recovery."""
        self.lose = 1
        api = self.fx.lab.resources[self.state["id"]]["apis"][0]
        inner, moved = api.transport, {"done": False}

        def transport(request):
            record = (self.bot.get("_protections") or [{}])[0]
            absent = record.get("absent")
            if (urlsplit(request.full_url).path == "/api/v5/account/positions" and absent and not moved["done"]
                    and absent["reads"] >= protection.ABSENT_READS + 1
                    and protection.ABSENT_SECONDS - 5 <= self.fx.now - absent["first_at"] < protection.ABSENT_SECONDS):
                moved["done"] = True
                self.fx.now += 6  # the check takes its time: its later read of the stop is past the limit
            return inner(request)
        api.transport = transport
        self.run_season(until=lambda: bool(self.events("protective_not_created")), limit=40)
        self.assertTrue(moved["done"])
        codes = [e["code"] for e in self.state["events"] if e["code"] in {"protective_not_created", "protective_intent", "protective_recovery"}]
        self.assertEqual(codes[-2:], ["protective_not_created", "protective_intent"])  # the new stop in the same ensure
        self.assertFalse(self.events("protective_recovery"))
        self.run_season(until=lambda: (self.bot.get("protection") or {}).get("status") == "ARMED", limit=5)
        self.assertEqual((self.bot.get("protection") or {}).get("status"), "ARMED")

    def test_pp03_a_positive_ack_whose_get_lags_is_ours_with_no_verdict(self):
        """RC2 (PP-03): OKX ACKs the stop with its algoId, and its GET answers 51603 for 70 s while it is listed pending:
        ours (no foreign_algo_order), no verdict, armed once the GET shows it."""
        self.lag = 70
        self.run_season(until=lambda: (self.bot.get("protection") or {}).get("status") == "ARMED", limit=40)
        self.assertEqual((self.bot.get("protection") or {}).get("status"), "ARMED")
        record = self.bot["_protections"][0]
        self.assertEqual(record["ack_algo_id"], self.live_algos()[0]["algoId"])
        self.assertFalse(self.events("protective_not_created"))
        self.assertFalse([e for e in self.events("exchange_check_failed") if e["data"].get("reason") == "foreign_algo_order"])
        self.assertFalse(self.bot["_manual_review"])

    def test_pp03_a_positive_ack_never_gets_not_created_but_the_recovery(self):
        """RC2 (PP-03): OKX ACKs the stop with an algoId, then neither lists nor shows it for 60 s: no verdict "not created"
        (that could place a second stop), the recovery: the close."""
        self.vanish = 1
        self.run_season(until=lambda: bool(self.events("protective_recovery")), limit=40)
        self.assertFalse(self.events("protective_not_created"))
        self.assertEqual(len(self.events("protective_recovery")), 1)
        self.assertTrue(self.bot["_protections"][0]["ack_algo_id"])
        self.run_season(limit=40)
        self.assertEqual(self.fake.units, 0)
        self.assertFalse(self.bot["_manual_review"])

    def test_pp03_a_lost_ack_with_the_stop_listed_pending_keeps_the_wait(self):
        """RC2 (PP-03): the ACK is lost, OKX created the stop and lists it pending, its GET lags 70 s: no verdict (that
        would place a second stop); its algoId joins the owned ones; armed once the GET shows it."""
        self.lose_ack, self.lag = 1, 70
        self.run_season(until=lambda: (self.bot.get("protection") or {}).get("status") == "ARMED", limit=40)
        self.assertEqual((self.bot.get("protection") or {}).get("status"), "ARMED")
        self.assertFalse(self.events("protective_not_created"))
        self.assertEqual(len(self.live_algos()), 1)
        self.assertEqual(self.bot["_protections"][0]["ack_algo_id"], self.live_algos()[0]["algoId"])
        self.assertFalse(self.bot["_manual_review"])

    def test_p05_a_refusal_51278_closes_reduce_only(self):
        self.refused("51278")


class StopNeverCreatedShort(StopNeverCreated):
    SIDE = "short"

    def setUp(self):
        recipe = json.loads(outcomes.RECIPE.read_text(encoding="utf-8"))
        recipe["rules"]["side"] = self.SIDE
        import tempfile
        from pathlib import Path
        self.recipes = tempfile.TemporaryDirectory()
        self.addCleanup(self.recipes.cleanup)
        path = Path(self.recipes.name) / "recipe.json"
        path.write_text(json.dumps(recipe), encoding="utf-8")
        with patch.object(outcomes, "RECIPE", path):
            super().setUp()

    test_p04_no_verdict_before_three_reads_and_60_s = None


class StopIntentNotJournalled(StopNeverCreated):
    """p03: a lasting journal fault from the append of the stop's intent: PAUSED with the record journalled in memory and
    never POSTed. Once the fault clears, the Resume (close-only) reads 51603 on it: the verdict, then the close."""

    def test_p03_a_journal_fault_on_the_intent_then_resume_closes(self):
        fx, lab, state, fake = self.fx, self.fx.lab, self.state, self.fake
        fault = {"on": False}
        append = ops_storage.SegmentedJournal.append

        def failing(journal, event, *, critical=False):
            if event.get("code") == "protective_intent":
                fault["on"] = True
            if fault["on"]:
                raise StorageBlocked("disk_reserve_blocks_writes_not_close")
            return append(journal, event, critical=critical)
        selected = lab.chosen_profiles(["arena-a"])
        with patch.object(ops_storage.SegmentedJournal, "append", failing):
            lab.prepare = lambda *a, **k: None
            lab.idle = lambda s, r, seconds: setattr(fx, "now", fx.now + 5)
            lab.worker(state, selected, False)
        self.assertEqual(state["status"], "PAUSED")
        self.assertNotEqual(fake.units, 0)
        self.assertFalse(self.live_algos())  # no stop on the venue
        self.assertTrue(any(r.get("send_started") and r.get("receipt") is None for r in self.bot["_protections"]))
        del lab.prepare
        original = lab.prepare

        def prepared(run, profiles, recovery):  # the Resume's own client answers 51603 for the stop OKX never saw
            result = original(run, profiles, recovery)
            self.wrap(lab.resources[run["id"]]["apis"][0])
            return result
        lab.prepare = prepared
        lab.resume(lab.data["generation"], state["id"])
        state = lab.load(state["id"])
        lab.idle = lambda s, r, seconds: setattr(fx, "now", fx.now + 5)
        lab.worker(state, selected, True)
        self.assertEqual(state["status"], "DONE")
        self.assertEqual(fake.units, 0)
        self.assertEqual(len([e for e in state["events"] if e["code"] == "protective_not_created"]), 1)
        self.assertFalse(self.live_algos())  # close-only: no new stop, the reduce-only close instead

    test_p04_a_lost_post_ends_in_not_created_and_a_new_stop = None
    test_p04_no_verdict_before_three_reads_and_60_s = None
    test_p04_the_local_stop_meanwhile_closes_reduce_only_without_waiting_for_the_verdict = None
    test_p05_a_refusal_51280_closes_reduce_only = None
    test_p05_a_refusal_51278_closes_reduce_only = None
    test_pp01_a_refusal_without_our_client_id_closes_reduce_only = None
    test_pp01_a_refusal_as_a_top_level_code_closes_reduce_only = None
    test_pp01_a_lasting_unlisted_refusal_gets_one_replacement_then_the_recovery = None
    test_pp02_a_verdict_on_the_second_read_places_the_new_stop_in_that_check = None
    test_pp03_a_positive_ack_whose_get_lags_is_ours_with_no_verdict = None
    test_pp03_a_lost_ack_with_the_stop_listed_pending_keeps_the_wait = None
    test_pp03_a_positive_ack_never_gets_not_created_but_the_recovery = None


if __name__ == "__main__":
    unittest.main()
