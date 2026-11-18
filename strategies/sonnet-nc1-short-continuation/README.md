# NC1: short on continued decline (ETH, BTC)

**Status:** Research summary, no candidate

- **Author:** "Sonnet", a fictional name of an AI author session; model Claude Opus 5.5 (`claude-opus-5-5`).
- **Date:** pre-registered and run on 2026-11-17. No recipe was frozen.
- **Instruments:** OKX ETH-USDT-SWAP and BTC-USDT-SWAP.
- **Side:** short only. A long branch ("buy the flush") was dropped before any result was seen, because another author covered that idea.
- **Holding horizon:** 8 or 24 hours.

## Idea

H10-ETH (see `sonnet-h10-eth`) trades only when the price is above its 20-day average, and it was idle almost all of the Live period. NC1 asked what works in the opposite regime. When the price is below its 20-day average and falls sharply, does the fall continue? The idea is the mirror image of H10: sell passively (post-only) a little above the signal price, catching a small bounce, then hold for 8 or 24 hours. In an earlier test (H13, taker orders) short ETH after a 4-hour fall in a downtrend had been positive in 2024 to mid-2025 but about zero over 2020 to mid-2025.

## Exact rules

These are the 8 tested configurations, written as schema-4 recipes for the official evaluator. No JSON is included because none was frozen.

Common to all:

| Field | Value |
|---|---|
| Side | `short` |
| Regime | `sma_gap_bps(1d, 20) < 0` (price below the average of the last 20 closed daily closes) |
| Order | `post_only` limit sell, offset `0.25 x rms_bar_bps(1h, 168)` above the signal close, `timeout_seconds` 7200 |
| Stop loss / take profit | 500 bps / 1000 bps |
| Cooldown / size | 600 s / `size_pct` 11 |

Grid: 2 instruments x 2 triggers x 2 holds = 8 configurations.

- Trigger T60: `return_bps(1m, 60) <= -3.0 x rms_bar_bps(1h, 168)`, the exact mirror of the H10 threshold.
- Trigger T240: `return_bps(1m, 240) <= -5.0 x rms_bar_bps(1h, 168)`, a 4-hour fall of at least about 2.5 typical 4-hour moves.
- Hold: `max_hold_seconds` 28800 (H8) or 86400 (H24).
- Controls (not selectable): short every day at 00:00 UTC (`utc_minute_of_day <= 0`) in the same regime, with the same order and exits, held 24 hours. One per instrument.

Selection rule, pre-registered before any run. The selection used only E2a, 2024-01-01 to 2025-06-30: the runs ended on 2025-06-30, so the tail was not computed. E1 (2020 to 2023) was a contrast. A configuration needed all of:

- (a) at least 40 closed trades;
- (b) net above 0 and stress above 0;
- (c) median absolute gross move per trade of at least 28 bps (2 x the 14 bps taker round trip);
- (d) at least 4 active quarters (5 or more trades each), with at least 60 % of them positive;
- (e) a better net per trade than its control;
- (f) E1 with stress not worse than -5 USDT.

At most one finalist would then get one look at the whole development history including the tail (gate K).

## Results

E2a = 2024-01-01 to 2025-06-30, the selection period. All runs used the official evaluator with execution model `v4-path5s-2`. Money in USDT on a 100 USDT account.

| Configuration | E2a trades | Net / stress | bps per trade | t | Median abs. gross, bps | Positive / active quarters | Post-only fill share | E1 trades / stress | Failed checks |
|---|---|---|---|---|---|---|---|---|---|
| ETH T60 H8 | 178 | -2.15 / -3.89 | -12.9 | -0.54 | 177 | 2/6 | 0.92 | 379 / -2.57 | b, d, e |
| ETH T60 H24 | 132 | -3.76 / -5.05 | -27.9 | -0.76 | 297 | 2/6 | 0.90 | 311 / +1.74 | b, d, e |
| ETH T240 H8 | 121 | +6.00 / +4.82 | +50.8 | +1.71 | 170 | 5/6 | 0.91 | 285 / -8.96 | f |
| ETH T240 H24 | 98 | +2.30 / +1.35 | +25.3 | +0.54 | 259 | 5/6 | 0.90 | 243 / -12.46 | f |
| BTC T60 H8 | 147 | -4.50 / -5.64 | -34.6 | -2.04 | 129 | 0/6 | 0.89 | 388 / -13.68 | b, d, f |
| BTC T60 H24 | 101 | -0.66 / -1.45 | -7.5 | -0.24 | 200 | 3/6 | 0.88 | 306 / -13.78 | b, d, f |
| BTC T240 H8 | 113 | -2.58 / -3.45 | -24.8 | -1.48 | 141 | 1/6 | 0.92 | 275 / -9.44 | b, d, f |
| BTC T240 H24 | 83 | -3.27 / -3.90 | -41.0 | -1.51 | 211 | 2/6 | 0.92 | 230 / -10.43 | b, d, e, f |
| Control ETH | 160 | +0.28 / -1.29 | -2.4 | +0.07 | 216 | 3/6 | 0.84 | 348 / -9.68 | not selectable |
| Control BTC | 121 | -3.52 / -4.45 | -39.8 | -1.57 | 141 | 1/6 | 0.84 | 332 / -5.58 | not selectable |

Net in USDT and the mean bps per trade can differ in sign (control ETH), because trade sizes differ after lot rounding.

**Verdict:** 0 of 8 configurations admitted, no finalist. The tail (2025-07 to 2026-10) was never opened for this family. **Gate K:** not reached. **Gate D:** none.

## Why it is not enough

- **BTC: no effect.** Continuation shorts lost in every BTC configuration in E2a (-7.5 to -41 bps per trade), and E1 with stress was worse than -9 USDT.
- **ETH: only the 4-hour trigger, and only recently.** ETH T240 H8 passed every E2a check (+50.8 bps per trade, t 1.71, 5 of 6 quarters positive) but lost -8.96 USDT with stress in E1. Over the whole period up to 2025-06 it was about zero: net -0.18, stress -4.23, and the lowest maker variant -1.29. In 2020 to 2023, falls in a downtrend bounced back; in 2024 to 2025 they kept going. That is the same split seen earlier in H13.
- **Costs.** Post-only orders filled on 88 to 92 % of signals. The lowest maker variant (every fill charged as a taker) was negative in every configuration.
- **A crowded family.** Shorts in a downtrend had already been tried by H13 (16 configurations) and by another author's pullback short. Any later plus in this family is weak evidence.
- **The post-hoc split did not choose anything.** Using the team's shared regime classifier, the author found ETH shorts after a fall positive in calm declines in both epochs in 3 of 4 configurations, and mostly negative in stormy declines (bounces and short squeezes). This was found after the selection and was not used for it.

## If you want to continue

- **Test the calm-decline idea properly.** Pre-register "short ETH after a sharp fall only in calm declines" and judge it once, on the unopened tail (2025-07-01 to 2026-10-02) or on data after 2026-10-02.
- **Try taker entries.** A team-wide lesson from this cycle: post-only entries on continuation signals suffer strong adverse selection. The unfilled orders were often the best trades (in another author's tests, missed entries would have made +82 to +227 bps, filled ones -8 to -78).
- **Try SOL.** In the two families that came closest in this cycle, SOL looked more promising than BTC.
- **Count the family.** Count all related short tests together when you judge significance.

## Reproduce

No frozen recipe exists, so there is no JSON here, but the configurations run on the official evaluator. To rebuild one, start from the H10 recipe in `strategies/sonnet-h10-eth/recipe.json` and make these changes:

- set `side` to `short`;
- use the trigger above as the first entry condition (for example `return_bps` with `tf` `1m`, `n` 240, `op` `lte`, `ref` `rms_bar_bps` `1h` 168, `mult` -5.0);
- set the regime condition `sma_gap_bps` `op` to `lt`;
- set `max_hold_seconds` to 28800 or 86400.

Then run the selection period only:

```
py -3.12 -X utf8 -B tools/team_eval.py run --recipe <your-nc1-recipe>.json --author you --label NC1-ETH-S-T240-H8 --set dev --end 2025-06-30 --store <store>/ETH-USDT-SWAP --out runs/nc1_eth_s_t240_h8
```

The E2a and E1 columns above split the evaluator's trade ledger by entry date (E2a 2024-01-01 to 2025-06-30, E1 2020-01-01 to 2023-12-31). The author used their own selection script for this. The data store is built as for H10 (see `sonnet-h10-eth`); the evaluator files used were `tools/team_eval.py` `b377f01a...` and `tools/team_eval_v4.py` `c6a440c4...`.
