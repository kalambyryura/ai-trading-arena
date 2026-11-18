"""Public, correctly rehashed DTOs remain untrusted contract input."""
from copy import deepcopy
import unittest
from trading_lab import news_features as news

T = 1800000000000
def fixture():
    draft = {"source_id":"fictional-source","source_url":"https://news.example.org/1","source_item_id":"one",
        "revision":1,"supersedes":None,"published_ms":T,"received_ms":T+100,"processed_ms":T+200,
        "assets":["BTC"],"asset_mapping":"EXPLICIT","text":"FICTIONAL BTC notice","duplicate_of":None,"contradicts":[]}
    journal = news.NewsJournal().append(draft)
    return journal,journal.snapshot(as_of_ms=T+2000,watermark_seq=1,target_id=journal.records()[0]["record_id"],asset="BTC",feed_status="COMPLETE"),draft

def rehash(snapshot):
    snapshot["snapshot_sha256"] = news.digest({k:v for k,v in snapshot.items() if k!="snapshot_sha256"})
    return snapshot

class PublicSnapshotBoundaryTests(unittest.TestCase):
    def test_rehashed_processed_future_rejected(self):
        _,snapshot,_ = fixture()
        snapshot["evidence"][0]["processed_ms"] = T+3000
        with self.assertRaises(news.ContractError): news.make_payload(rehash(snapshot))

    def test_rehashed_degraded_feed_with_known_flag_rejected(self):
        _,snapshot,_ = fixture()
        snapshot["feed_status"] = "DEGRADED"
        with self.assertRaises(news.ContractError): news.make_payload(rehash(snapshot))

    def test_rehashed_missing_target_rejected(self):
        _,snapshot,_ = fixture()
        snapshot["target_id"] = "a"*64
        with self.assertRaises(news.ContractError): news.make_payload(rehash(snapshot))

    def test_rehashed_unknown_asset_mapping_rejected(self):
        _,snapshot,_ = fixture()
        snapshot["evidence"][0].update(asset_mapping="UNKNOWN",assets=[])
        with self.assertRaises(news.ContractError): news.make_payload(rehash(snapshot))

    def test_rehashed_text_digest_mismatch_rejected(self):
        _,snapshot,_ = fixture()
        snapshot["evidence"][0]["text"] = "rewritten source assertion"
        with self.assertRaises(news.ContractError): news.make_payload(rehash(snapshot))

    def test_rehashed_claim_of_source_authentication_rejected(self):
        _,snapshot,_ = fixture()
        snapshot["source_claims_verified"] = True
        with self.assertRaises(news.ContractError): news.make_payload(rehash(snapshot))

    def test_answer_must_cite_target_not_only_related_evidence(self):
        journal,_,draft = fixture()
        older = journal.records()[0]["record_id"]
        journal = journal.append({**draft,"source_item_id":"two","text":"FICTIONAL second BTC notice",
            "published_ms":T+500,"received_ms":T+600,"processed_ms":T+700})
        target = journal.records()[-1]["record_id"]
        snapshot = journal.snapshot(as_of_ms=T+2000,watermark_seq=2,target_id=target,asset="BTC",feed_status="COMPLETE")
        payload = news.make_payload(snapshot)
        answer = {"payload_sha256":news.digest(payload),"model_revision":"SYNTHETIC-NO-MODEL","event_kind":"OTHER",
            "novelty":"NEW","contradiction":"UNKNOWN","materiality":.5,"evidence_ids":[older],"duplicate_of":None,"contradicts":[]}
        with self.assertRaises(news.ContractError):
            news.derive_features(payload,answer,requested_ms=T+2000,completed_ms=T+2100,decision_ms=T+2200)

if __name__ == "__main__":
    unittest.main()
