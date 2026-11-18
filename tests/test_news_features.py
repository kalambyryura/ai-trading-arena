"""Bounded synthetic contract tests, no model/network/market replay."""
from copy import deepcopy
import io
import json
from pathlib import Path
import socket
import unittest
from unittest.mock import patch

from trading_lab import news_features as news

HERE = Path(__file__).resolve().parent
FIXTURE = json.loads((HERE/"fixtures"/"news_features_synthetic.json").read_text(encoding="utf-8"))
T = FIXTURE["base_ms"]

class NewsContractTests(unittest.TestCase):
    def setUp(self):
        self.draft = deepcopy(FIXTURE["drafts"]["security"])
        self.journal = news.NewsJournal().append(self.draft)
        self.target = self.journal.records()[-1]["record_id"]
        blocker = patch.object(socket.socket,"connect",side_effect=AssertionError("Network forbidden"))
        blocker.start(); self.addCleanup(blocker.stop)

    def snapshot(self, journal=None, target=None, as_of=T+2000, watermark=None, **kwargs):
        journal = journal or self.journal
        return journal.snapshot(as_of_ms=as_of,watermark_seq=len(journal.rows) if watermark is None else watermark,
            target_id=target or self.target,asset="BTC",feed_status=kwargs.pop("feed_status","COMPLETE"),**kwargs)

    def answer(self,payload,**changes):
        answer = {"payload_sha256":news.digest(payload),"model_revision":"SYNTHETIC-NO-MODEL",
            "event_kind":"SECURITY","novelty":"NEW","contradiction":"UNKNOWN","materiality":.4,
            "evidence_ids":[payload["data"]["target_id"]],"duplicate_of":None,"contradicts":[]}
        answer.update(changes)
        return answer

    def derive(self,payload,answer=None,**times):
        return news.derive_features(payload,answer or self.answer(payload),requested_ms=times.get("requested_ms",T+2000),
            completed_ms=times.get("completed_ms",T+2100),decision_ms=times.get("decision_ms",T+2200))

    def test_one_bounded_batch_and_features_are_not_orders(self):
        payload = news.make_payload(self.snapshot())
        self.assertEqual(len(payload["questions"]),4)
        self.assertLessEqual(len(news.canonical(payload).encode()),news.MAX_PAYLOAD_BYTES)
        features = self.derive(payload)
        self.assertEqual(features["status"],"PARTIAL")
        self.assertEqual(features["execution_permission"],"NONE")
        self.assertFalse(features["probabilities_calibrated"])
        self.assertNotIn("order",features)

    def test_future_publication_and_received_processed_order_rejected(self):
        for values in ({"published_ms":T+500},{"processed_ms":T},{"received_ms":True},{"published_ms":float("nan")}):
            with self.subTest(values=values),self.assertRaises(ValueError):
                news.NewsJournal().append({**self.draft,**values})

    def test_future_processed_watermark_cannot_enter_model_context(self):
        with self.assertRaisesRegex(news.ContractError,"future_inputs"):
            self.snapshot(as_of=T+150)

    def test_old_watermark_snapshot_unchanged_after_late_and_equal_time_append(self):
        before = self.snapshot(as_of=T+200,watermark=1)
        later = {**self.draft,"source_item_id":"other","text":"FICTIONAL BTC update","published_ms":T+200,"received_ms":T+200,"processed_ms":T+200}
        appended = self.journal.append(later)
        self.assertEqual(before,self.snapshot(appended,as_of=T+200,watermark=1))
        future = {**later,"source_item_id":"future","published_ms":T+5000,"received_ms":T+5100,"processed_ms":T+5200}
        self.assertEqual(before,self.snapshot(appended.append(future),as_of=T+200,watermark=1))

    def test_revision_is_append_only_and_no_retroactive_rewrite(self):
        before = self.snapshot(watermark=1)
        revision = {**self.draft,"revision":2,"supersedes":self.target,"published_ms":T+2500,"received_ms":T+2600,"processed_ms":T+2700,"text":"FICTIONAL correction: the BTC incident claim was withdrawn."}
        updated = self.journal.append(revision)
        self.assertEqual(before,self.snapshot(updated,watermark=1))
        old_now = self.snapshot(updated,as_of=T+3000)
        self.assertIn("SUPERSEDED_AS_OF",old_now["reasons"])
        new_now = self.snapshot(updated,target=updated.records()[-1]["record_id"],as_of=T+3000)
        self.assertEqual(new_now["status"],"KNOWN")
        with self.assertRaises(news.ContractError): self.journal.append(self.draft)

    def test_backdated_processing_rejected(self):
        with self.assertRaisesRegex(news.ContractError,"backdated"):
            self.journal.append({**self.draft,"source_item_id":"other","processed_ms":T+199})

    def test_missing_publication_or_feed_is_unknown_not_zero_news(self):
        journal = news.NewsJournal().append({**self.draft,"published_ms":None})
        for snapshot in (self.snapshot(journal,target=journal.records()[0]["record_id"]),self.snapshot(feed_status="UNKNOWN"),self.snapshot(feed_status="DEGRADED")):
            self.assertEqual(snapshot["status"],"UNKNOWN")
            with self.assertRaises(news.ContractError): news.make_payload(snapshot)
        empty = news.NewsJournal().snapshot(as_of_ms=T,watermark_seq=0,target_id="absent",asset="BTC",feed_status="UNKNOWN")
        self.assertEqual(empty["status"],"UNKNOWN")

    def test_ambiguous_asset_mapping_cannot_become_known_btc_feature(self):
        for mapping,assets in (("INFERRED",["BTC"]),("UNKNOWN",[]),("EXPLICIT",["ETH"])):
            journal = news.NewsJournal().append({**self.draft,"asset_mapping":mapping,"assets":assets})
            snapshot = self.snapshot(journal,target=journal.records()[0]["record_id"])
            self.assertIn("ASSET_MAPPING_UNKNOWN",snapshot["reasons"])

    def test_ttl_inclusive_and_not_refreshed_by_processing_time(self):
        self.assertEqual(self.snapshot(as_of=T+news.DEFAULT_TTL_MS)["status"],"KNOWN")
        self.assertEqual(self.snapshot(as_of=T+news.DEFAULT_TTL_MS+1)["status"],"UNKNOWN")
        late = {**self.draft,"received_ms":T+news.DEFAULT_TTL_MS+100,"processed_ms":T+news.DEFAULT_TTL_MS+200}
        journal = news.NewsJournal().append(late)
        self.assertIn("SOURCE_TTL_EXPIRED",self.snapshot(journal,target=journal.records()[0]["record_id"],as_of=T+news.DEFAULT_TTL_MS+300)["reasons"])

    def test_response_delayed_beyond_ttl_is_unknown_not_zero(self):
        payload = news.make_payload(self.snapshot())
        features = self.derive(payload,completed_ms=T+news.DEFAULT_TTL_MS+1,decision_ms=T+news.DEFAULT_TTL_MS+1)
        self.assertEqual(features["status"],"UNKNOWN")
        self.assertIsNone(features["features"]["materiality"])
        self.assertEqual(features["features"]["event_kind"],"UNKNOWN")

    def test_future_or_wrong_input_answer_rejected(self):
        payload = news.make_payload(self.snapshot())
        with self.assertRaises(news.ContractError): self.derive(payload,completed_ms=T+2500)
        with self.assertRaises(news.ContractError): self.derive(payload,requested_ms=T+1999)
        with self.assertRaises(news.ContractError): self.derive(payload,self.answer(payload,payload_sha256="other"))

    def test_materiality_finite_bounded_real_not_bool(self):
        payload = news.make_payload(self.snapshot())
        for value in (float("nan"),float("inf"),float("-inf"),-0.001,1.001,True,".5"):
            with self.subTest(value=value),self.assertRaises(news.ContractError):
                self.derive(payload,self.answer(payload,materiality=value))
        self.assertEqual(self.derive(payload,self.answer(payload,materiality=0))["features"]["materiality"],0)

    def test_exact_duplicate_cannot_be_new_or_inflate_novelty(self):
        duplicate = {**self.draft,"source_id":"fictional-source-b","source_item_id":"reprint","duplicate_of":self.target,"published_ms":T+500,"received_ms":T+600,"processed_ms":T+700}
        journal = self.journal.append(duplicate)
        payload = news.make_payload(self.snapshot(journal,target=journal.records()[-1]["record_id"]))
        with self.assertRaises(news.ContractError): self.derive(payload)
        features = self.derive(payload,self.answer(payload,novelty="DUPLICATE",duplicate_of=self.target))
        self.assertEqual(features["duplicate_of"],self.target)

    def test_contradiction_requires_visible_past_evidence(self):
        correction = {**self.draft,"source_item_id":"denial","text":"FICTIONAL source denies earlier BTC security claim.","contradicts":[self.target],"published_ms":T+500,"received_ms":T+600,"processed_ms":T+700}
        journal = self.journal.append(correction)
        payload = news.make_payload(self.snapshot(journal,target=journal.records()[-1]["record_id"]))
        for refs in ([],["future-id"]):
            with self.assertRaises(news.ContractError): self.derive(payload,self.answer(payload,contradiction="YES",contradicts=refs))
        self.assertEqual(self.derive(payload,self.answer(payload,contradiction="YES",contradicts=[self.target]))["features"]["contradiction"],"YES")

    def test_untrusted_instruction_text_is_data_and_trade_command_output_rejected(self):
        journal = self.journal.append(deepcopy(FIXTURE["drafts"]["injection"]))
        payload = news.make_payload(self.snapshot(journal,target=journal.records()[-1]["record_id"]))
        self.assertIn("BUY BTC NOW",payload["data"]["evidence"][0]["text"])
        self.assertIn("untrusted quoted data",payload["policy"])
        self.assertEqual(self.derive(payload)["execution_permission"],"NONE")
        with self.assertRaises(news.ContractError): self.derive(payload,{**self.answer(payload),"action":"BUY"})

    def test_bounds_and_no_mutable_aliases(self):
        self.draft["text"]="later mutation"
        records = self.journal.records(); records[0]["text"]="other mutation"
        self.assertNotEqual(self.journal.records()[0]["text"],"other mutation")
        with self.assertRaises(news.ContractError): news.NewsJournal().append({**self.draft,"text":"x"*(news.MAX_TEXT_BYTES+1)})
        with self.assertRaises(news.ContractError): news.NewsJournal().append({**self.draft,"source_url":"https://127.0.0.1/private"})
        tampered = self.snapshot(); tampered["evidence"][0]["text"]="altered"
        with self.assertRaises(news.ContractError): news.make_payload(tampered)

    def test_append_chain_tampering_is_rejected(self):
        row = json.loads(self.journal.rows[0]); row["text"]="rewritten old news"
        with self.assertRaises(news.ContractError): news.NewsJournal((news.canonical(row),))

    def test_rehashed_import_cannot_bypass_draft_schema(self):
        for changes in ({"revision":0},{"assets":["FAKE"]},{"source_url":"https://localhost/private"}):
            row = {**json.loads(self.journal.rows[0]),**changes}
            row["record_id"] = news.digest({k:v for k,v in row.items() if k!="record_id"})
            with self.subTest(changes=changes),self.assertRaises(news.ContractError):
                news.NewsJournal((news.canonical(row),))

    def test_context_remains_bounded_with_more_past_news(self):
        journal = self.journal
        for i in range(5):
            journal = journal.append({**self.draft,"source_item_id":"item"+str(i),"text":"FICTIONAL distinct BTC item "+str(i),
                "published_ms":T+300+i,"received_ms":T+400+i,"processed_ms":T+500+i})
        payload = news.make_payload(self.snapshot(journal,target=journal.records()[-1]["record_id"]))
        self.assertEqual(len(payload["data"]["evidence"]),news.MAX_CONTEXT)
        self.assertEqual(len(payload["questions"]),4)
        self.assertLessEqual(len(news.canonical(payload).encode()),news.MAX_PAYLOAD_BYTES)

    def test_unknown_semantic_scores_preserve_abstention(self):
        payload = news.make_payload(self.snapshot())
        result = self.derive(payload,self.answer(payload,event_kind="UNKNOWN",novelty="UNKNOWN",contradiction="UNKNOWN",materiality=None))
        self.assertEqual(result["status"],"UNKNOWN")
        self.assertIsNone(result["features"]["materiality"])

    def test_contradiction_cannot_cite_itself(self):
        payload = news.make_payload(self.snapshot())
        with self.assertRaises(news.ContractError):
            self.derive(payload,self.answer(payload,contradiction="YES",contradicts=[self.target]))

if __name__ == "__main__":
    unittest.main()
