"""Small synthetic unit checks only; no economic evaluation or parameter search."""
from dataclasses import asdict, replace
from copy import deepcopy
import unittest

from trading_lab.ikigai_g1_reference import (Bar, Detector, HOUR, Instrument, Params, Pivot, Position,
                       Quote, Setup, Signal, bar_exit, plan_entry, time_exit)


def fixture(mirror=False, knot_changes=None):
    """76 fictional H1 bars; no actual market/account data."""
    knots = {32: 110, 40: 100, 48: 108, 56: 102, 64: 106, 72: 103.5,
             74: 105, 75: 108.3}
    knots.update(knot_changes or {})
    prices = [80 + i * .8 for i in range(32)]
    points = sorted(knots.items())
    for i in range(32, 76):
        if i in knots:
            prices.append(knots[i])
        else:
            a, b = next((a, b) for a, b in zip(points, points[1:])
                        if a[0] < i < b[0])
            prices.append(a[1] + (b[1] - a[1]) * (i - a[0]) / (b[0] - a[0]))
    bars = [Bar(i * HOUR, (i + 1) * HOUR + 1, x, x + .05, x - .05, x)
            for i, x in enumerate(prices)]
    if mirror:
        bars = [replace(b, open=220 - b.open, close=220 - b.close,
                        high=220 - b.low, low=220 - b.high) for b in bars]
    return bars


def run(bars, cutoff=10**10, has_position=False):
    detector = Detector(.01)
    for b in bars:
        detector.on_bar(b, b.received_at, cutoff, has_position)
    return detector


def signal_from(event):
    payload = event["signal"]
    raw = dict(payload["setup"])
    raw["anchors"] = tuple(Pivot(**x) for x in raw["anchors"])
    raw["context"] = tuple(tuple(x) for x in raw["context"])
    return Signal(Setup(**raw), payload["decided_at"], payload["signal_bar_ts"])


class CausalityTests(unittest.TestCase):
    def setUp(self):
        self.bars = fixture()

    def test_signal_after_e_confirmed_and_after_setup_bar(self):
        d = run(self.bars)
        setups = [x for x in d.events if x['kind'] == 'SETUP']
        signals = [x for x in d.events if x['kind'] == 'ENTRY_SIGNAL']
        self.assertEqual(len(setups), 1)
        self.assertEqual(len(signals), 1)
        s = signal_from(signals[0])
        self.assertEqual(s.setup.anchors[-1].open_ts, 72 * HOUR)
        self.assertEqual(s.setup.anchors[-1].confirmed_at, 75 * HOUR + 1)
        self.assertEqual(s.decided_at, 76 * HOUR + 1)
        self.assertLess(s.setup.created_bar_ts, s.signal_bar_ts)
        self.assertTrue(all(x[0] <= s.setup.anchors[0].open_ts for x in s.setup.context))

    def test_every_prefix_matches_full_run_past(self):
        full = run(self.bars).events
        for length in range(1, len(self.bars) + 1):
            partial = run(self.bars[:length]).events
            expected = [e for e in full if e['at'] <= self.bars[length-1].received_at]
            self.assertEqual(partial, expected)

    def test_future_does_not_modify_previous_events(self):
        d = run(self.bars)
        before = deepcopy(d.events)
        extra = Bar(76 * HOUR, 77 * HOUR + 1, 108, 109, 90, 91)
        d.on_bar(extra, extra.received_at, 10**10)
        self.assertEqual(d.events[:len(before)], before)

    def test_anchors_frozen_while_waiting(self):
        d = run(self.bars[:-1])
        old = asdict(d.pending)
        b = replace(self.bars[-1], close=106, open=106, high=107, low=105)
        d.on_bar(b, b.received_at, 10**10)
        self.assertEqual(asdict(d.pending), old)

    def test_short_mirror_has_same_causal_structure(self):
        events = run(fixture(mirror=True)).events
        signal = signal_from(next(e for e in events if e['kind'] == 'ENTRY_SIGNAL'))
        self.assertEqual(signal.setup.side, 'short')
        self.assertLess(signal.setup.target, signal.setup.trigger)
        self.assertLess(signal.setup.trigger, signal.setup.stop)

    def test_wick_break_without_close_is_not_entry(self):
        bars = self.bars[:-1] + [replace(self.bars[-1], open=107, close=107,
                                        high=109, low=105)]
        self.assertFalse(any(e['kind'] == 'ENTRY_SIGNAL' for e in run(bars).events))

    def test_e_violation_wins_over_breakout_same_bar(self):
        bars = self.bars[:-1] + [replace(self.bars[-1], low=102)]
        events = run(bars).events
        self.assertTrue(any(e['kind'] == 'INVALIDATED' for e in events))
        self.assertFalse(any(e['kind'] == 'ENTRY_SIGNAL' for e in events))

    def test_late_detection_not_retroactive_entry(self):
        events = run(fixture(knot_changes={74: 109, 75: 110})).events
        self.assertTrue(any(e.get('reason') == 'ALREADY_BROKEN_BEFORE_SETUP' for e in events))
        self.assertFalse(any(e['kind'] == 'ENTRY_SIGNAL' for e in events))

    def test_incomplete_future_late_bars_unknown(self):
        for bar, now in [(replace(self.bars[-1], complete=False), self.bars[-1].received_at),
                         (self.bars[-1], self.bars[-1].close_ts-1),
                         (replace(self.bars[-1], received_at=self.bars[-1].close_ts+121),
                          self.bars[-1].close_ts+121)]:
            d = run(self.bars[:-1])
            events = d.on_bar(bar, now, 10**10)
            self.assertEqual(events[0]['kind'], 'UNKNOWN_DATA')
            self.assertIsNone(d.pending)

    def test_gap_invalidates_without_interpolating(self):
        d = run(self.bars[:-1])
        b = replace(self.bars[-1], open_ts=76*HOUR, received_at=77*HOUR+1)
        events = d.on_bar(b, b.received_at, 10**10)
        self.assertEqual(events[0]['kind'], 'GAP_RESET')
        self.assertFalse(any(e['kind'] == 'ENTRY_SIGNAL' for e in events))

    def test_duplicate_revision_not_replayed(self):
        d = run(self.bars[:-1])
        events = d.on_bar(self.bars[-2], self.bars[-2].received_at, 10**10)
        self.assertEqual(events[0]['kind'], 'UNKNOWN_REVISION_OR_OUT_OF_ORDER')
        self.assertIsNone(d.pending)

    def test_running_triangle_outside_chosen_subset(self):
        events = run(fixture(knot_changes={48: 111})).events
        self.assertTrue(any(e.get('reason') == 'NOT_ORDINARY_CONTRACTION' for e in events))
        self.assertFalse(any(e['kind'] == 'ENTRY_SIGNAL' for e in events))

    def test_no_h4_context_no_setup(self):
        bars = list(self.bars)
        for i in range(32):
            bars[i] = replace(bars[i], open=105, high=105.05, low=104.95, close=105)
        events = run(bars).events
        self.assertTrue(any(e.get('reason') == 'HTF_CONTEXT_UNKNOWN_OR_MISMATCH' for e in events))
        self.assertFalse(any(e['kind'] == 'ENTRY_SIGNAL' for e in events))

    def test_admission_position_unknown_cooldown_block(self):
        t = self.bars[-1].received_at
        for cutoff, occupied, expected in [(t, False, 'ADMISSION_CLOSED'),
                                          (t+1, True, 'POSITION_EXISTS'),
                                          (t+1, None, 'UNKNOWN_POSITION')]:
            events = run(self.bars, cutoff, occupied).events
            self.assertTrue(any(e.get('reason') == expected for e in events))
            self.assertFalse(any(e['kind'] == 'ENTRY_SIGNAL' for e in events))
        d = run(self.bars[:-1]); d.cooldown_until = t+1
        events = d.on_bar(self.bars[-1], t, t+1)
        self.assertTrue(any(e.get('reason') == 'COOLDOWN' for e in events))

    def test_flat_cancels_setup_and_requires_new_anchor_chain(self):
        d = run(self.bars[:-1]); t = self.bars[-2].received_at
        d.mark_flat(t)
        self.assertIsNone(d.pending)
        self.assertEqual(d.cooldown_until, t + 4*HOUR)
        _, reason = d._make_setup(t + 5*HOUR, self.bars[-2])
        self.assertEqual(reason, 'ANCHORS_PREDATE_LAST_FLAT')

    def test_ambiguous_dual_pivot_is_not_a_triangle_anchor(self):
        bars = [Bar(i*HOUR, (i+1)*HOUR+1, 100, 101, 99, 100) for i in range(5)]
        bars[2] = replace(bars[2], high=120, low=80)
        d = run(bars)
        self.assertTrue(any(e['kind'] == 'AMBIGUOUS_PIVOT' for e in d.events))
        self.assertEqual(d.pivots, [])

    def test_apex_expiry_no_delayed_entry(self):
        d = run(self.bars[:-1])
        for i in range(75, 81):
            b = Bar(i*HOUR, (i+1)*HOUR+1, 105, 105.2, 104.8, 105)
            d.on_bar(b, b.received_at, 10**10)
        self.assertTrue(any(e['kind'] == 'EXPIRED' for e in d.events))
        self.assertFalse(any(e['kind'] == 'ENTRY_SIGNAL' for e in d.events))


class SizingAndExitTests(unittest.TestCase):
    def setUp(self):
        self.signal = signal_from(next(e for e in run(fixture()).events
                                       if e['kind'] == 'ENTRY_SIGNAL'))
        t = self.signal.decided_at
        self.quote = Quote(t+1, t+2, 108.30, 108.32, 100)
        self.instrument = Instrument(.01, .01, 1, 1)

    def plan(self, quote=None, instrument=None, cutoff=10**10):
        return plan_entry(self.signal, quote or self.quote,
                          instrument or self.instrument, 100, cutoff)

    def test_valid_plan_risk_cap_and_floor(self):
        p = self.plan()
        self.assertEqual(p['kind'], 'OFFLINE_ENTRY_PLAN')
        self.assertLessEqual(p['modeled_stop_loss_with_reserve'], .5)
        self.assertLessEqual(p['notional'], 30)
        self.assertEqual(p['contracts'] % self.instrument.lot_contracts, 0)
        self.assertGreaterEqual(p['net_reward_risk'], 1.5)
        self.assertFalse(p['guaranteed_max_loss'])
        self.assertFalse(p['can_submit_orders'])
        self.assertEqual(p['target'], 118.15)  # no binary-float extra tick haircut

    def test_quote_before_decision_stale_spread_cutoff_chase(self):
        t = self.signal.decided_at
        cases = [(replace(self.quote, ts=t), 10**10, 'STALE_OR_PREDECISION_QUOTE'),
                 (replace(self.quote, received_at=t+10), 10**10, 'STALE_OR_PREDECISION_QUOTE'),
                 (replace(self.quote, ask=109), 10**10, 'SPREAD'),
                 (self.quote, self.quote.received_at, 'ADMISSION_OR_SETUP_EXPIRED'),
                 (replace(self.quote, bid=110, ask=110.02), 10**10, 'RETURNED_INSIDE_OR_CHASE')]
        for q, cutoff, expected in cases:
            self.assertEqual(self.plan(q, cutoff=cutoff)['reason'], expected)

    def test_min_lot_no_upsize_and_book_cap(self):
        p = self.plan(instrument=replace(self.instrument, min_contracts=1000))
        self.assertEqual(p['reason'], 'BELOW_MINIMUM_NO_UPSIZE')
        q = replace(self.quote, book_base_size=.02)
        self.assertEqual(self.plan(quote=q)['reason'], 'BELOW_MINIMUM_NO_UPSIZE')

    def test_short_size_with_same_risk_bound(self):
        s = signal_from(next(e for e in run(fixture(mirror=True)).events
                             if e['kind'] == 'ENTRY_SIGNAL'))
        q = Quote(s.decided_at+1, s.decided_at+2, 111.68, 111.70, 100)
        p = plan_entry(s, q, self.instrument, 100, 10**10)
        self.assertEqual(p['kind'], 'OFFLINE_ENTRY_PLAN')
        self.assertLessEqual(p['modeled_stop_loss_with_reserve'], .5)

    def test_tick_change_requires_new_setup(self):
        self.assertEqual(self.plan(instrument=replace(self.instrument, tick_size=.1))['reason'],
                         'TICK_METADATA_DRIFT')

    def test_stop_before_target_same_bar_and_prefill_unknown(self):
        pos = Position('long', 80*HOUR, 103, 118, 108)
        b = Bar(80*HOUR, 81*HOUR+1, 108, 119, 102, 109)
        self.assertEqual(bar_exit(pos, [b], b.received_at), 'STOP_MODEL_FIRST_IF_AMBIGUOUS')
        self.assertEqual(bar_exit(replace(pos, actual_fill_at=80*HOUR+2), [b],
                                  b.received_at), 'UNKNOWN_POST_FILL_COVERAGE')

    def test_earlier_stop_not_overwritten_by_later_rally(self):
        pos = Position('long', 80*HOUR, 103, 118, 108)
        a = Bar(80*HOUR, 81*HOUR+1, 108, 109, 102, 104)
        b = Bar(81*HOUR, 82*HOUR+1, 104, 119, 104, 118)
        self.assertEqual(bar_exit(pos, [a, b], b.received_at), 'STOP_MODEL_FIRST_IF_AMBIGUOUS')

    def test_failed_breakout_and_postfill_gap(self):
        pos = Position('long', 80*HOUR, 103, 118, 108)
        a = Bar(80*HOUR, 81*HOUR+1, 108, 109, 106, 107)
        b = Bar(81*HOUR, 82*HOUR+1, 107, 108, 106, 107)
        self.assertEqual(bar_exit(pos, [a], a.received_at), 'HOLD')
        self.assertEqual(bar_exit(pos, [a, b], b.received_at), 'FAILED_BREAKOUT_CLOSES')
        b = replace(b, open_ts=82*HOUR, received_at=83*HOUR+1)
        self.assertEqual(bar_exit(pos, [a, b], b.received_at), 'UNKNOWN_POST_FILL_GAP')

    def test_authored_time_exit_independent_of_admission(self):
        pos = Position('long', 80*HOUR, 103, 118, 108)
        self.assertIsNone(time_exit(pos, 85*HOUR))  # a cutoff at85h does not close it
        self.assertEqual(time_exit(pos, 104*HOUR), 'AUTHORED_MAX_HOLD')
        self.assertEqual(time_exit(pos, 81*HOUR, owner_stop=True), 'OWNER_STOP_CLOSE_OWNED')

    def test_earlier_failed_break_not_reclassified_as_later_stop(self):
        pos = Position('long', 80*HOUR, 103, 118, 108)
        a = Bar(80*HOUR, 81*HOUR+1, 108, 109, 106, 107)
        b = Bar(81*HOUR, 82*HOUR+1, 107, 108, 106, 107)
        c = Bar(82*HOUR, 83*HOUR+1, 107, 109, 102, 106)
        self.assertEqual(bar_exit(pos, [a, b, c], c.received_at), 'FAILED_BREAKOUT_CLOSES')

    def test_missing_first_full_bar_or_stale_exit_data_unknown(self):
        pos = Position('long', 80*HOUR, 103, 118, 108)
        b = Bar(81*HOUR, 82*HOUR+1, 108, 109, 106, 108)
        self.assertEqual(bar_exit(pos, [b], b.received_at), 'UNKNOWN_POST_FILL_COVERAGE')
        a = replace(b, open_ts=80*HOUR, received_at=81*HOUR+1)
        self.assertEqual(bar_exit(pos, [a], a.received_at+121), 'UNKNOWN_POST_FILL_COVERAGE')

    def test_late_batch_does_not_reclassify_known_old_stop_as_time_exit(self):
        pos = Position('long', 80*HOUR, 103, 118, 108)
        bars = [Bar(i*HOUR, (i+1)*HOUR+1, 108, 110, 107, 109)
                for i in range(80, 104)]
        bars[0] = replace(bars[0], low=102)
        self.assertEqual(bar_exit(pos, bars, bars[-1].received_at),
                         'STOP_MODEL_FIRST_IF_AMBIGUOUS')
        bars[0] = replace(bars[0], low=107)
        self.assertEqual(bar_exit(pos, bars, bars[-1].received_at), 'AUTHORED_MAX_HOLD')


if __name__ == '__main__':
    unittest.main()
