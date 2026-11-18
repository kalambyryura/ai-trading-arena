"""R2.1.14 (R2113-RESUME-SETTLED, D163 p. 2; season a037504b, four bots settled before the pause showed PENDING after the
Resume). A bot STOPPED with its accounting confirmed before a pause keeps it over the Resume; one GET of its position and
pending orders on the Resume; a foreign row is the manual review; transient codes the usual failure path for at most 120 s.
The mixed fixture of R2.1.12 (two bots, each on its own strict fake subaccount) by composition. Temp roots only."""
from copy import deepcopy
from urllib.parse import urlsplit
import unittest

import test_r2112_mix as mix
from trading_lab.battle_review import build_review
from trading_lab.exchange_api import venue_error
from trading_lab.match_journal import restore_events


class SettledBeforeThePause(unittest.TestCase):
    def setUp(self):
        self.mix = mix.MixedSeason("test_mix_a_snapshot_of_another_contract_is_refused")
        self.mix.setUp()
        self.addCleanup(self.mix.doCleanups)
        m = self.mix
        m.start()
        m.steps(2)
        lab = m.fx.lab
        lab.stop(lab.data["generation"], m.state["id"], m.b["id"])  # the owner stops one bot: it closes and settles
        m.steps(30, until=lambda: m.b["status"] == "STOPPED")
        self.assertEqual(m.b["status"], "STOPPED")
        self.assertTrue(m.b["accounting_reconciled"])
        self.assertNotEqual(m.e["status"], "STOPPED")
        self.accounting = deepcopy(m.b["accounting"])
        self.confirmed = len(self.of(m.b, "accounting_confirmed"))

    def of(self, bot, code):
        return [e for e in self.mix.state["events"] if e["code"] == code and e.get("bot_id") == bot["id"]]

    def resume(self):
        m = self.mix
        lab = m.resume_after_restart()  # the process ends, a new one loads the checkpoint, the owner resumes
        lab.prepare(m.state, lab.chosen_profiles(["arena-a", "arena-b"]), True)
        return lab

    def finish(self):
        m = self.mix
        m.steps(60, until=lambda: m.state["status"] == "DONE")
        self.assertEqual(m.state["status"], "DONE", [e["code"] for e in m.state["events"][-12:]])

    def armed(self, fake, path, make_row):
        return self.mix.add_row(fake, path, make_row)

    def failing(self, fake, times):
        """Its positions GET answers HTTP 503 / 50001 `times` times from now (a new process's clients bind the transport
        when they are made, so this goes before the Resume)."""
        original, left = fake.transport, {"n": times}

        def transport(request):
            if urlsplit(request.full_url).path == "/api/v5/account/positions" and left["n"]:
                left["n"] -= 1
                raise venue_error("http_503", http_status=503, okx_code="50001")
            return original(request)
        fake.transport = transport

    def test_a_settled_bot_keeps_its_accounting_over_the_resume(self):
        m = self.mix
        self.resume()
        self.assertTrue(m.b["accounting_reconciled"])  # not reset by the Resume
        self.finish()
        self.assertEqual(len(self.of(m.b, "settled_account_verified")), 1)
        self.assertEqual(len(self.of(m.b, "accounting_confirmed")), self.confirmed)  # no second settlement
        self.assertEqual(m.b["accounting"], self.accounting)
        self.assertTrue(m.e["accounting_reconciled"])  # the other bot settles after the Resume as before
        reads = [r for r in m.btc.requests[m.marks[id(m.btc)]:] if r.get_method() == "GET"
                 and urlsplit(r.full_url).path in {"/api/v5/account/positions", "/api/v5/trade/orders-pending"}]
        self.assertTrue(reads)  # the one account read of the settled bot
        self.assertEqual([r for r in m.btc.requests[m.marks[id(m.btc)]:] if r.get_method() == "POST"], [])
        full = deepcopy(m.fx.lab.load(m.state["id"]))
        restore_events(m.fx.lab.path / (full["id"] + ".events.jsonl"), full, full=True)
        review = build_review(full)
        row = next(p for p in review["participants"] if p["id"] == m.b["id"])
        self.assertEqual(row["quality"]["settlement"]["status"], "SETTLED")
        self.assertEqual(row["costs"]["source"], "actual_bills_and_cash_reconciled")
        self.assertIn("settled_account_verified", {e["code"] for e in review["events"]})  # exported

    def assert_not_kept(self):
        """RC-09: a STOPPED bot without a confirmed accounting, or one in the manual review, is not taken for settled."""
        m = self.mix
        self.resume()
        self.assertNotIn("_settled_check", m.b)
        self.assertFalse(m.b["accounting_reconciled"])
        self.finish()
        self.assertFalse(self.of(m.b, "settled_account_verified"))
        full = deepcopy(m.fx.lab.load(m.state["id"]))
        restore_events(m.fx.lab.path / (full["id"] + ".events.jsonl"), full, full=True)
        row = next(p for p in build_review(full)["participants"] if p["id"] == m.b["id"])
        self.assertEqual(row["quality"]["settlement"]["status"], "PENDING")

    def test_a_stopped_bot_without_a_confirmed_accounting_is_not_kept(self):
        self.mix.b["accounting_reconciled"] = False  # STOPPED, its settlement never confirmed before the pause
        self.assert_not_kept()

    def test_a_stopped_bot_in_the_manual_review_is_not_kept(self):
        self.mix.b["_manual_review"] = True  # STOPPED and in the manual review before the pause
        self.assert_not_kept()

    def test_a_foreign_position_on_a_settled_subaccount_is_the_manual_review(self):
        m = self.mix
        armed = self.armed(m.btc, "/api/v5/account/positions", mix.FOREIGN_POSITION(mix.ETH))
        self.resume()
        armed["on"] = True
        self.finish()
        self.assertTrue(m.b["_manual_review"])
        self.assertIn("foreign_or_unsupported_position",
                      {e["data"].get("reason") for e in self.of(m.b, "exchange_check_failed")})
        self.assertFalse(self.of(m.b, "settled_account_verified"))
        self.assertTrue(m.b["accounting_reconciled"])  # our own trades stay settled; the season is not complete
        self.assertTrue(m.state["incomplete"])
        self.assertEqual([r for r in m.btc.requests[m.marks[id(m.btc)]:] if r.get_method() == "POST"], [])

    def test_a_foreign_pending_order_on_a_settled_subaccount_is_the_manual_review(self):
        m = self.mix
        armed = self.armed(m.btc, "/api/v5/trade/orders-pending", mix.FOREIGN_ORDER(mix.BTC))
        self.resume()
        armed["on"] = True
        self.finish()
        self.assertTrue(m.b["_manual_review"])
        self.assertIn("foreign_or_unbounded_pending_orders",
                      {e["data"].get("reason") for e in self.of(m.b, "exchange_check_failed")})

    def test_transient_codes_retry_and_then_leave_the_accounting_unconfirmed(self):
        m = self.mix
        self.failing(m.btc, 10 ** 6)  # the settled bot's subaccount does not answer its positions GET
        self.resume()
        self.finish()
        self.assertGreaterEqual(len(self.of(m.b, "exchange_check_failed")), 2)  # read again, step after step
        self.assertEqual(len(self.of(m.b, "settled_account_unverified")), 1)
        self.assertFalse(m.b["accounting_reconciled"])  # as before R2.1.14
        self.assertFalse(m.b["_manual_review"])
        # RC2 (RC-04, probe P4): one outage is one degraded window, and the STOPPED bot keeps no block or failure count
        self.assertEqual(len(self.of(m.b, "degraded_window_started")), 1)
        self.assertEqual([e["data"]["outcome"] for e in self.of(m.b, "degraded_window_ended")], ["stopped"])
        self.assertIsNone(m.b.get("admission_blocked"))
        self.assertEqual(m.b["_api_failures"], 0)

    def test_transient_codes_then_a_read_keep_the_accounting(self):
        m = self.mix
        self.failing(m.btc, 2)
        self.resume()
        self.finish()
        self.assertEqual(len(self.of(m.b, "settled_account_verified")), 1)
        self.assertTrue(m.b["accounting_reconciled"])
        self.assertEqual(m.b["_api_failures"], 0)
        self.assertEqual(len(self.of(m.b, "degraded_window_started")), 1)  # RC-04: one window for the two failures
        self.assertEqual([e["data"]["outcome"] for e in self.of(m.b, "degraded_window_ended")], ["recovered"])


if __name__ == "__main__":
    unittest.main()
