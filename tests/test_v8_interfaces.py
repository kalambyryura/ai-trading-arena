"""Integrated read-only HTTP and standalone release gates, exclusively fake/temp."""
from copy import deepcopy
import http.client
import json
import shutil
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from trading_lab.arena import Arena, write
from trading_lab.arena_strategies import ArenaError
from trading_lab.arena_jev import make_payload
from trading_lab.battle_review import build_review, verify_bundle, validate_request
from trading_lab.event_replay import build_packet
from trading_lab.spectator_export import export, gate, receipt_template
from trading_lab.strategy_contract import payload_of
from trading_lab.web_panel import Panel, Server
from tests.test_replay_evidence_integration import paper_fixture
from tests.test_management_integration import recipe

ROOT = Path(__file__).resolve().parents[1]


class V8InterfaceTests(unittest.TestCase):
    def approved(self, packet):
        return {**receipt_template(packet), "privacy": "PASS", "rights": "APPROVED", "rights_reference": "Synthetic fixture; no exchange data",
                "owner_release": "APPROVED", "owner_reference": "Offline release test", "access": "PUBLIC"}

    def test_export_requires_separate_owner_privacy_rights_and_delay(self):
        packet = build_packet(paper_fixture())
        for changes in ({"owner_release": "PENDING"}, {"rights": "PENDING"}, {"privacy": "PENDING"},
                        {"rights_reference": ""}, {"access": "PRIVATE"}, {"release_after_ms": 101}):
            with self.subTest(changes=changes), self.assertRaises(ArenaError): gate(packet, {**self.approved(packet), **changes}, 100)
        self.assertTrue(gate(packet, self.approved(packet), 100)["allowed"])
        corrupt = deepcopy(packet); corrupt["events"][0]["code"] = "changed"
        with self.assertRaises(ArenaError): gate(corrupt, self.approved(packet), 100)

    def test_standalone_package_contains_no_owner_bootstrap_or_network_and_no_overwrite(self):
        source = paper_fixture(); source["fixture_only"] = True
        packet = build_packet(source)
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "public-display"
            result = export(packet, self.approved(packet), target, ROOT / "web", 100)
            self.assertFalse(result["public_hosting"])
            html = (target / "index.html").read_text(encoding="utf-8")
            self.assertNotIn("__SPECTATOR_DATA__", html)
            self.assertNotIn('src="arena.js"', html)
            self.assertNotIn("panel-token", html)
            self.assertLess(html.index('src="review-model.js"'), html.index('src="replay-labels.js"'))
            self.assertNotIn("fetch(", (target / "spectator.js").read_text(encoding="utf-8"))
            self.assertNotIn("/assets/", (target / "arena-coliseum.js").read_text(encoding="utf-8"))
            with self.assertRaises(ArenaError): export(packet, self.approved(packet), target, ROOT / "web", 100)

    def test_private_request_exact_numeric_payload_is_checked_and_prompt_injection_rejected(self):
        state = paper_fixture(); item = state["strategies"][0]
        payload = make_payload([{"id": item["id"], "recipe_sha256": item["recipe_sha256"], "side": "long", "kind": "entry",
            "position_btc": 0, "local_checks_passed": True, "rules": item["rules"], "features": {"spread_bps": 1},
            "checks": [{"feature": "spread_bps", "op": "lte", "expected": 5, "actual": 1, "passed": True}]}])
        import hashlib
        body = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()
        state["events"].append({"seq": len(state["events"])+1, "at": state["events"][-1]["at"], "code": "jev_request", "bot_id": None, "step": 2,
            "data": {"payload": payload, "payload_sha256": hashlib.sha256(body).hexdigest(), "request_at_ms": 1000}})
        self.assertNotIn("request_payloads", build_review(state))
        self.assertTrue(verify_bundle(build_review(state, include_request_payloads=True))["verified"])
        payload["state"]["c0"]["rules"]["private_prompt"] = "PRIVATE_MARKER"
        with self.assertRaises(ArenaError): build_review(state, include_request_payloads=True)

    def test_integrated_http_new_pages_dtos_and_workshop_tools_have_zero_transports(self):
        with tempfile.TemporaryDirectory() as directory, patch("urllib.request.urlopen", side_effect=AssertionError("transport forbidden")):
            root = Path(directory)
            shutil.copytree(ROOT / "web", root / "web")
            def forbidden(*args, **kwargs): raise AssertionError("financial/provider call forbidden")
            arena = Arena(root, background=False, feed_factory=forbidden)
            journal = paper_fixture()
            arena.data["seasons"].append(journal["id"])
            write(arena.path / (journal["id"] + ".json"), journal)
            with Server(Panel(root, launcher=forbidden, balance_reader=forbidden), 0, arena=arena) as server:
                thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
                try:
                    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
                    for path in ("/review", "/league", "/workshop", "/workshop-tools-ui.js", "/api/league", "/api/ops/state",
                                 "/api/review/paper/"+journal["id"], "/api/replay/paper/"+journal["id"]):
                        connection.request("GET", path)
                        response = connection.getresponse(); body = response.read().decode()
                        self.assertEqual(response.status, 200, (path, body[:200]))
                        self.assertNotIn("PRIVATE_MARKER", body)
                    headers = {"Content-Type": "application/json", "Origin": server.origin, "X-Panel-Token": server.panel.token}
                    connection.request("POST", "/api/workshop/tools", json.dumps({"strategy": payload_of(recipe())}), headers)
                    response = connection.getresponse(); result = json.loads(response.read())
                    self.assertEqual(response.status, 200, result)
                    self.assertEqual(set(result) & {"lint", "explain", "lots", "trials", "compare"}, {"lint", "explain", "lots", "trials", "compare"})
                    connection.request("POST", "/api/workshop/tools", '{}', {"Content-Type": "application/json"})
                    response = connection.getresponse(); response.read(); self.assertEqual(response.status, 403)
                    for path in ("/.env.local", "/runtime/league_v1/manifest.json", "/api/replay/paper/../escape"):
                        connection.request("GET", path); response = connection.getresponse(); response.read(); self.assertEqual(response.status, 404)
                    connection.close()
                finally:
                    server.shutdown(); thread.join(3)

    def test_private_v2_metadata_is_numeric_and_bounded_with_legacy_wire_supported(self):
        state = paper_fixture(); item = state["strategies"][0]
        candidate = {"id": item["id"], "recipe_sha256": item["recipe_sha256"], "side": "long", "kind": "entry",
            "position_btc": 0, "local_checks_passed": True, "rules": item["rules"], "features": {"spread_bps": 1},
            "checks": [{"feature": "spread_bps", "op": "lte", "expected": 5, "actual": 1, "passed": True,
                        "path": "/all/0", "side": "long", "rhs_feature": None}]}
        payload = make_payload([candidate])
        validate_request(payload)
        for column, value in ((5, "PRIVATE_MARKER"), (6, "PRIVATE_MARKER"), (7, "PRIVATE_MARKER")):
            altered = deepcopy(payload); altered["state"]["c0"]["checks"][0][column] = value
            with self.assertRaises(ArenaError): validate_request(altered)
        altered = deepcopy(payload); altered["state"]["v2_rule_format"]["entry"] = "PRIVATE_MARKER"
        with self.assertRaises(ArenaError): validate_request(altered)
        from trading_lab.arena_strategies import catalogue_defaults
        v1 = {**candidate, "rules": catalogue_defaults()[0]["rules"]}
        legacy = deepcopy(payload)
        legacy["state"].pop("v2_rule_format")
        legacy["state"]["c0"].pop("rule_schema_version")
        legacy["state"]["c0"]["checks"] = [c[:5] for c in legacy["state"]["c0"]["checks"]]
        legacy["state"]["check_columns"] = legacy["state"]["check_columns"][:5]
        legacy["state"]["policy"] = make_payload([v1])["state"]["policy"]
        validate_request(legacy)
        validate_request(make_payload([v1]))
        validate_request(make_payload([v1, candidate]))

    def test_persisted_sorted_json_has_explicit_unknown_wire_and_new_wire_is_verified(self):
        import hashlib
        state = paper_fixture(); item = state["strategies"][0]
        payload = make_payload([{"id": item["id"], "recipe_sha256": item["recipe_sha256"], "side": "long", "kind": "entry",
            "position_btc": 0, "local_checks_passed": True, "rules": item["rules"], "features": {"spread_bps": 1}, "checks": []}])
        wire = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        data = {"payload": json.loads(json.dumps(payload, sort_keys=True)), "payload_sha256": hashlib.sha256(wire.encode()).hexdigest(), "request_at_ms": 1000}
        event = {"seq": len(state["events"])+1, "at": state["events"][-1]["at"], "code": "jev_request", "bot_id": None, "step": 2, "data": data}
        state["events"].append(event)
        old = build_review(state, include_request_payloads=True)
        self.assertEqual(old["request_payloads"][0]["wire_hash_status"], "NOT_RETAINED")
        self.assertTrue(verify_bundle(old)["verified"])
        self.assertFalse(verify_bundle(old)["request_wire_integrity_complete"])
        self.assertTrue(old["request_payload_limitations"])
        data["payload_wire_json"] = wire
        new = build_review(state, include_request_payloads=True)
        self.assertEqual(new["request_payloads"][0]["wire_hash_status"], "VERIFIED_WIRE")
        self.assertTrue(verify_bundle(new)["request_wire_integrity_complete"])
        data["payload_wire_json"] = wire.replace('"spread_bps":1', '"spread_bps":2')
        with self.assertRaises(ArenaError): build_review(state, include_request_payloads=True)


if __name__ == "__main__": unittest.main()
