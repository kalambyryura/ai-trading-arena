"""Pure typed market SHADOW proposal. No API/key reader or default transport.

Frozen numeric/template projections are the boundary. A SHADOW label cannot
execute, veto or delay an order. Fake execution tests never touch real ledgers.
"""
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import math
import re

MODEL = "typesafe/jev-1.13"
MAX_BYTES = 8000
MAX_AGE_MS = 8000
RUBRIC = "market-shadow-v1"
FEATURES = {"spread_bps", "return_1m_bps", "return_5m_bps", "sma_gap_bps", "rsi_14",
            "range_position_20", "volume_ratio_5", "atr_14_bps", "realized_vol_20_bps"}
FAMILIES = {"trend_pullback", "dip_rebound", "momentum", "mean_reversion", "capitulation_rebound"}


def canonical(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def finite(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def project_market(context):
    required = {"source", "instrument", "snapshot_sha256", "feature_version", "observed_server_ms", "bars", "features"}
    if not isinstance(context, dict) or set(context) != required:
        raise ValueError("market_projection_shape")
    if context["source"] not in {"OKX_LIVE_PUBLIC", "OKX_DEMO_PUBLIC", "FIXTURE"} or context["instrument"] != "BTC-USDT-SWAP":
        raise ValueError("market_source")
    if not re.fullmatch(r"[0-9a-f]{64}", context["snapshot_sha256"]) or context["feature_version"] != "market-features-v1":
        raise ValueError("market_hash_or_features")
    if type(context["observed_server_ms"]) is not int:
        raise ValueError("server_time")
    rows = context["bars"]
    if not isinstance(rows, list) or not 20 <= len(rows) <= 60:
        raise ValueError("closed_bar_count")
    previous = None
    for row in rows:
        if not isinstance(row, list) or len(row) != 6 or not all(finite(v) for v in row):
            raise ValueError("closed_bar_shape")
        stamp, opening, high, low, close, volume = row
        if (type(stamp) is not int or stamp % 60000 or stamp + 60000 > context["observed_server_ms"]
                or (previous is not None and stamp != previous + 60000)
                or min(opening, high, low, close) <= 0 or volume < 0
                or high < max(opening, close) or low > min(opening, close) or high < low):
            raise ValueError("bar_not_closed_or_contiguous")
        previous = stamp
    features = context["features"]
    if not isinstance(features, dict) or not features or set(features) - FEATURES:
        raise ValueError("feature_projection_shape")
    if any(value is not None and not finite(value) for value in features.values()):
        raise ValueError("feature_not_numeric")
    return deepcopy(context)


def project_setups(setups):
    if not isinstance(setups, list) or not 1 <= len(setups) <= 6:
        raise ValueError("setup_count")
    result = []
    for index, setup in enumerate(setups):
        if not isinstance(setup, dict) or set(setup) != {"slot", "family", "side", "horizon_seconds"}:
            raise ValueError("setup_projection_shape")
        if setup["slot"] != "s" + str(index) or setup["family"] not in FAMILIES or setup["side"] not in {"long", "short"}:
            raise ValueError("setup_enum")
        horizon = setup["horizon_seconds"]
        if horizon is not None and (type(horizon) is not int or not 1 <= horizon <= 604800):
            raise ValueError("setup_horizon")
        result.append(deepcopy(setup))
    return result


def questions_for(setups):
    questions = {
        "regime": {"type": "choice", "instructions": "Label only the closed market sequence under this rubric; do not predict future prices.",
                   "criteria": {"up": "Directional progress upward.", "down": "Directional progress downward.",
                                "range": "Alternating direction with limited net progress.", "transition": "Recent directional change.",
                                "unknown": "Insufficient or conflicting evidence."}},
        "coherence": {"type": "score", "instructions": "Rate directional coherence of the closed sequence, independent of the setup slots.",
                      "criteria": ["No coherent direction.", "Weak direction.", "Mixed but discernible direction.",
                                   "Consistent direction.", "Strong sustained direction."]},
        "stalling": {"type": "noul", "instructions": "Does the last five-bar sequence show an earlier impulse losing progress?",
                     "criteria": {"true": "Recent ranges and closes show a slowing earlier impulse.",
                                  "false": "An earlier impulse is absent or has maintained progress."}},
    }
    for setup in setups:
        slot = setup["slot"]
        questions["fit_" + slot] = {"type": "choice", "instructions": "Label the shared public context against setup " + slot + "; use only the declared enum family and side.",
                                  "criteria": {"aligned": "Observed sequence supports the declared setup family.",
                                               "conflicted": "Observed sequence contradicts the declared setup family.",
                                               "unknown": "Cannot classify with available public evidence."}}
    return questions


@dataclass(frozen=True)
class ShadowPack:
    payload: dict
    body: bytes
    sha256: str


def build_pack(context, setups):
    market, slots = project_market(context), project_setups(setups)
    payload = {"model": MODEL, "provider": {"allow_fallbacks": False},
               "state": {"mode": "SHADOW", "rubric_version": RUBRIC, "market": market, "setups": slots},
               "questions": questions_for(slots)}
    body = canonical(payload)
    if len(body) > MAX_BYTES:
        raise ValueError("payload_over_cap_no_split")
    return ShadowPack(payload, body, hashlib.sha256(body).hexdigest())


def probability_map(value, expected):
    if not isinstance(value, dict) or set(value) != set(expected) or any(not finite(v) or not 0 <= v <= 1 for v in value.values()):
        raise ValueError("probabilities_shape")
    if abs(sum(value.values()) - 1) > 1e-6:
        raise ValueError("probabilities_sum")
    return value


def answer_for(question, answer):
    if not isinstance(answer, dict) or answer.get("type") != question["type"]:
        raise ValueError("answer_type")
    kind = question["type"]
    if kind == "noul":
        if set(answer) != {"type", "noul"} or not finite(answer["noul"]) or not 0 <= answer["noul"] <= 1:
            raise ValueError("noul_answer")
        return deepcopy(answer)  # Noul has no separate confidence field.
    if not finite(answer.get("confidence")) or not 0 <= answer["confidence"] <= 1:
        raise ValueError("confidence")
    if kind == "choice":
        if set(answer) != {"type", "choice", "probabilities", "confidence"} or answer.get("choice") not in question["criteria"]:
            raise ValueError("choice_answer")
        probabilities = probability_map(answer["probabilities"], question["criteria"])
        if probabilities[answer["choice"]] + 1e-9 < max(probabilities.values()):
            raise ValueError("choice_not_max_probability")
    else:
        levels = {str(i): value for i, value in enumerate(question["criteria"])}
        if set(answer) != {"type", "score", "legend", "probabilities", "confidence"} or answer.get("legend") != levels:
            raise ValueError("score_legend")
        probabilities = probability_map(answer["probabilities"], levels)
        mean = sum(int(i) * probability for i, probability in probabilities.items())
        if not finite(answer.get("score")) or abs(answer["score"] - mean) > 1e-6:
            raise ValueError("score_not_weighted_probability")
    return deepcopy(answer)


def consume(pack, response, *, elapsed_ms, source_sha256):
    result = {"labels": {}, "reason": "unknown", "execution_effect": "NONE", "allowed": [],
              "payload_sha256": pack.sha256, "reported_cost_usd": None,
              "input_tokens": None, "output_tokens": None}
    # Billing is a separate fact from semantic validity. Expired/malformed
    # answers still consume their reported cost and must not disappear from
    # aggregate accounting or get retried as though no request happened.
    usage = response.get("usage") if isinstance(response, dict) else None
    if isinstance(usage, dict):
        cost = usage.get("cost")
        if finite(cost) and cost >= 0:
            result["reported_cost_usd"] = cost
        for name in ("input_tokens", "output_tokens"):
            if type(usage.get(name)) is int and usage[name] >= 0:
                result[name] = usage[name]
    try:
        if not finite(elapsed_ms) or not 0 <= elapsed_ms <= MAX_AGE_MS:
            raise ValueError("answer_expired")
        if source_sha256 != pack.payload["state"]["market"]["snapshot_sha256"]:
            raise ValueError("observation_changed")
        if not isinstance(response, dict) or not isinstance(response.get("answers"), dict) or set(response["answers"]) != set(pack.payload["questions"]):
            raise ValueError("batch_question_set")
        if result["reported_cost_usd"] is None:
            raise ValueError("billing_unknown")
        for name in ("input_tokens", "output_tokens"):
            if result[name] is None:
                raise ValueError("usage_tokens_unknown")
        labels = {key: answer_for(question, response["answers"][key]) for key, question in pack.payload["questions"].items()}
        result.update(labels=labels, reason="typed_shadow_only")
    except (ValueError, KeyError, TypeError) as error:
        result["reason"] = str(error) if isinstance(error, ValueError) else "invalid_batch"
    return result


class FakeShadowEpisodeRunner:
    """Fixture runner only: one fake evaluation per episode, no hidden retry.

    A production owner-operated SHADOW service must provide a preregistered
    bounded ledger and use the existing send_once adapter explicitly. This class
    has neither credentials nor ledger-path access and cannot launch that service.
    """
    def __init__(self, fake_transport):
        if getattr(fake_transport, "is_fake", False) is not True:
            raise ValueError("fixture_transport_required")
        self.transport = fake_transport
        self.episodes = set()
        self.attempts = 0
        self.billing_unknown = False

    def evaluate(self, episode, pack):
        base = {"labels": {}, "allowed": [], "execution_effect": "NONE"}
        if self.billing_unknown:
            return {**base, "reason": "billing_unknown_blocks_next_batch"}
        if episode in self.episodes:
            return {**base, "reason": "episode_already_evaluated_no_retry"}
        if len(self.episodes) >= 128:
            return {**base, "reason": "fixture_episode_cap"}
        self.episodes.add(episode)
        self.attempts += 1
        try:
            response = self.transport(pack.body)
        except Exception:
            self.billing_unknown = True
            return {**base, "reason": "transport_failure_unknown_billing"}
        result = consume(pack, response, elapsed_ms=10,
                         source_sha256=pack.payload["state"]["market"]["snapshot_sha256"])
        self.billing_unknown = result["reported_cost_usd"] is None
        return result
