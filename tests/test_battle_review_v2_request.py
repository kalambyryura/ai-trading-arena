"""battle_review.validate_request accepts the V1 and V2 Jev gate payloads and still fails closed on tampering."""
from copy import deepcopy
import json
from pathlib import Path
import unittest

from tools import jev_gate_probe as probe
from trading_lab.arena_jev import make_payload
from trading_lab.arena_market import build_snapshot
from trading_lab.arena_strategies import ArenaError, validate_strategy
from trading_lab.battle_review import validate_request

FIXTURE = Path(__file__).parent / "fixtures" / "jev_gate_live5h.json"


def as_v2(raw):
    raw = deepcopy(raw)
    rules = raw["rules"]
    leaves = [{"feature": c["feature"], "op": c["op"], "value": c["value"]} for c in rules["entry"]["conditions"]]
    exits = rules["exit"]["conditions"]
    rules["entry"] = {rules["side"]: {"setup": None, "confirm": leaves[0] if len(leaves) == 1 else {"all": leaves},
                                      "invalidate": None, "expiry_bars": None}}
    rules["exit"] = None if not exits else (exits[0] if len(exits) == 1 else {"any": exits})
    rules.update(add_when=None, cost_checkpoint=None, reduce_when=None, reduce_pct=None)
    raw["strategy_schema_version"] = 2
    return raw


class ValidateRequestSchemaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = probe.load_fixture(FIXTURE)
        cls.decision = next(d for d in cls.fixture["decisions"] if d["author"] == "sonnet")
        cls.snapshot = build_snapshot(cls.decision["raw"], cls.decision["snapshot_at"])
        cls.v1 = validate_strategy(deepcopy(cls.fixture["recipes"]["sonnet"]))
        cls.v2 = validate_strategy(as_v2(cls.fixture["recipes"]["sonnet"]))

    def candidate(self, recipe, key="bot"):
        row = probe.candidate_for(recipe, self.snapshot)
        row["id"] = key
        return row

    def test_v1_payload_validates(self):
        validate_request(make_payload([self.candidate(self.v1)], "OKX_LIVE"))

    def test_v2_payload_with_path_side_and_rhs_columns_validates(self):
        payload = make_payload([self.candidate(self.v2)], "OKX_LIVE")
        self.assertEqual(payload["state"]["check_columns"][-3:], ["path", "side", "rhs_feature"])
        self.assertEqual(payload["state"]["c0"]["rule_schema_version"], 2)
        validate_request(payload)

    def test_mixed_v1_and_v2_batch_validates(self):
        validate_request(make_payload([self.candidate(self.v1, "a"), self.candidate(self.v2, "b")], "OKX_LIVE"))

    def test_tampering_still_fails_closed(self):
        payload = make_payload([self.candidate(self.v2)], "OKX_LIVE")
        tampered = []
        extra_column = deepcopy(payload)
        extra_column["state"]["check_columns"].append("private_note")
        tampered.append(extra_column)
        short_row = deepcopy(payload)
        short_row["state"]["c0"]["checks"][0] = short_row["state"]["c0"]["checks"][0][:5]
        tampered.append(short_row)
        bad_branch = deepcopy(payload)
        bad_branch["state"]["c0"]["checks"][0][6] = "sideways"
        tampered.append(bad_branch)
        bad_reference = deepcopy(payload)
        bad_reference["state"]["c0"]["checks"][0][7] = "private_prompt"
        tampered.append(bad_reference)
        wrong_schema = deepcopy(payload)
        wrong_schema["state"]["c0"]["rule_schema_version"] = 1
        tampered.append(wrong_schema)
        prose = deepcopy(payload)
        prose["state"]["c0"]["rules"]["private_prompt"] = "PRIVATE_MARKER"
        tampered.append(prose)
        for index, item in enumerate(tampered):
            with self.subTest(case=index):
                with self.assertRaises(ArenaError):
                    validate_request(item)


if __name__ == "__main__":
    unittest.main()
