# G1 "IKIGAI Triangle"

**Status:** Paper only

- **Author:** kalambyrYura (project owner, human). The idea comes from the public lessons of IKIGAI, a Russian-language YouTube channel that teaches Elliott waves with Fibonacci levels. Codex, the AI agent that ran the project before the Claude team, turned it into fixed rules; every number in those rules is Codex's choice, not the owner's or the source's. Later, the owner's AI assistant session (Claude Opus 5.5) analysed it and ran a follow-up test, and the team's platform engineer replayed it on history.
- **Spec fixed:** 2026-10-26 (spec sha256 `18ab6f5f2001d2300ce91650af31787f5c035c3f77ed340e11efcc35cc7718f8`). It was never frozen as a gate candidate.
- **Instrument:** BTC-USDT-SWAP (OKX linear perpetual), one position, 1× isolated.
- **Side:** long and short (mirror-image rules).
- **Holding horizon:** up to 24 hours.
- **Where it can run:** PAPER only. The code refuses Live and Demo, and it never traded real money.

## Idea

In this method, a move can be followed by a contracting triangle of five legs, A-B-C-D-E. The lesson's trade is to wait until the price breaks back beyond point B in the direction of the earlier move, then enter, with the stop beyond E. The target is roughly the height of the triangle's largest leg, measured from the breakout. The main source is IKIGAI's triangle lesson of 2024-03-11 (https://www.youtube.com/watch?v=pUjgoCw8Axo, in Russian). In IKIGAI's teaching the triangle stands in a specific place in a larger wave count (wave 4, B or X) and each of its legs is itself a zigzag. G1 deliberately automates only the outer shape: six turning points on hourly bars and a simple 4-hour trend filter. It does not check the inner zigzags or the wave count.

## Exact rules

There is no recipe JSON: G1 cannot be written in the team's recipe schema, which has no turning points, absolute price levels or risk-based sizing. The rules live in code, in `trading_lab/ikigai_g1_reference.py` (the reference) and `trading_lab/ikigai_g1.py` (the PAPER bridge). In short, for a long trade:

- **Bars:** 1-hour BTC-USDT-SWAP bars in UTC for the pattern. 4-hour bars are built from four completed hourly bars (00/04/08/…). No Heikin-Ashi.
- **Turning points:** a swing high is a unique high above the highs of the two bars on each side; a swing low mirrors it. A point counts only after the second bar to its right has closed.
- **Pattern:** the last six alternating points P0, A, B, C, D, E (high, low, high, low, high, low) must satisfy P0 > B > D > E > C > A, which is a contracting triangle. Each leg lasts at least 3 hours and moves at least 20 bps. P0 to E takes 12–72 hours. The last width, D–E, is 0.10–0.75 of the first, P0–A. The lines B–D and C–E meet after E, and no later than 24 hours after E.
- **Context:** the last three 4-hour bars that ended by the start of P0 have rising highs and rising lows, and the last close is at least 1% above the first.
- **Setup life:** until the earlier of 12 hours after the setup is fixed or the triangle's apex.
- **Entry:** trigger = B + max(2 ticks, 5 bps), rounded up. Enter when a 1-hour close crosses above the trigger and the previous close was not above it. A low below E before the entry cancels the setup. The entry needs a fresh quote after the decision, a spread of at most 10 bps, and a price no more than 50 bps past the trigger. One IOC attempt per setup.
- **Stop:** E − max(2 ticks, 10 bps), rounded down. It is never moved.
- **Target:** B + H, where H is the largest of the legs P0–A, A–B, B–C, C–D and D–E.
- **Size:** risk 0.5% of equity at the modelled stop fill. The model counts a 5 bps fee and 2 bps slippage per side, the spread, and a 10 bps funding reserve. The notional is at most 30% of equity and at most 10% of the size visible at the best price. The stop distance must be 20–500 bps, and the modelled net reward must be at least 1.5 times the modelled loss.
- **Exit:** the first of the stop, the target, two consecutive 1-hour closes back at or inside B (a failed breakout), or 24 hours after the first fill.
- **Cooldown:** 4 hours after the position is flat. All six points of the next pattern must come after that.
- **Short trades:** the mirror image of all of the above.

## Results

| Period | Trades | Net, USDT | Stress, USDT | Notes |
|---|---:|---:|---:|---|
| History, BTC 2020-01-11 → 2026-10-02 (the platform's replay of the reference code; indicative, not the official evaluator) | 0 | — | — | 59,202 hourly bars, the first 256 used as warm-up. 4,696 sequences of six alternating points were checked: 141 were contracting, 134 of those had a leg shorter than 3 hours, and 1, 2 and 4 failed the width, apex and 4-hour trend rules. 0 setups |
| PAPER | — | — | — | A PAPER pilot was prepared; no PAPER trade results are recorded in the project files |
| Live, Demo | — | — | — | Not allowed by the code |

**Gate K: FAIL** (indicative): 0 closed trades, and at least 100 are required.

**Gate D:** not submitted.

**Why there were no trades.** The "two bars on each side" rule marks a turning point about once every 3.7 hours on BTC. That is close to theory: on a pure random walk the same rule fires once every 3.56 bars. So "the last six points" are small wiggles, not the large triangle a person sees on the chart, and the "each leg at least 3 hours" rule then removes almost all of them. The detector and the rules look at different scales. Loosening the 3-hour rule would only trade noise.

### Follow-up test of the same method: the "golden pullback" (V2)

On 2026-10-30 the owner's assistant researched the IKIGAI method in depth and offered three mechanical variants. The owner chose V2 with two controls, and it was pre-registered before any run. V2 is not G1. It uses IKIGAI's other main setup, a pullback after an impulse:

- Waves on 1-hour BTC come from a causal zigzag: a turning point is confirmed when the hourly close moves 3 × ATR(14) away from the current extreme, with 1.5 × ATR for the smaller scale.
- After an impulse that made a new extreme, wait for a pullback of 0.382–0.786 of the impulse. Enter when the pullback low is confirmed on the smaller scale.
- The stop is 1 tick beyond the start of the impulse. With L as the pullback low, close half at L + 1.0 × the impulse length and move the stop to the entry, then close the rest at L + 1.618 × the impulse length.
- Long and short, with a risk of 0.5% of equity and no time exit.

| BTC 2020-01-01 → 2026-10-02 (author's research code; indicative gate K) | Trades | Net, USDT | Stress, USDT | Notes |
|---|---:|---:|---:|---|
| V2 golden pullback | 151 | −2.87 | −4.47 | Without the 10 best trades −13.94; mean gross R −0.002; 5 numeric gate K checks failed |
| Control C1: wave breakout | 316 | −4.50 | −7.85 | 6 checks failed |
| Control C2: the same pullback with a shifted band, 0.30–0.70 | 118 | +6.59 | +5.46 | 1 check failed: without the 10 best trades −3.64 |

V2 was rejected. Before costs it behaves like a random walk: the mean gross R is −0.002 (null model +0.001), 45.7% of trades win (null model 45%), and the 10 best trades make 22.7R (null model 22.7R). The control with the shifted band did at least as well, so the Fibonacci band added nothing. That control was not promoted, because picking a winner after the look would be selection bias, and it fails the "without the 10 best trades" check anyway. Long trades made +3.80 USDT and short trades −6.67, which fits the general upward drift of 2020–2026 rather than the method.

## Why it is not enough

- **No trades in 6.7 years of history**, so there is nothing to evaluate.
- **Only the outer shape is coded.** The parts of the method that may carry the skill are not encoded: the larger wave count on daily, weekly and monthly charts, the inner zigzags, deciding whether this is wave 4, B or X, and skipping doubtful trades.
- **Triangles are rare even at the right scale.** On random-walk data, a triangle at the scale the eye sees appears about 6 times in 6.75 years, and about 23 times at half that scale; pullback setups appear 219–337 times. A triangle-only bot is unlikely to reach the 100 trades that gate K needs.
- **No outside evidence:** the team's literature review found no published evidence that Fibonacci levels or Elliott waves give an edge.
- **The mechanical golden pullback had no edge** either (see above).
- **It does not fit the recipe schema** used by the Live executor. A Live path was estimated at about 3–4 days of platform work, review and a Demo check, and was not started.

## If you want to continue

- **Define waves by size, not by two bars:** for example a reversal of k × ATR(14) at close, with k = 3 as in common practice. Count setups on history first, without looking at profit; aim for at least 100 trades before any profit test.
- **Try a higher timeframe:** the pattern on 4-hour bars and the context on daily bars.
- **Add the larger context:** take the triangle only after a clear move of the larger scale (a triangle as wave 4 before wave 5, or as B before C).
- **Use the planned control:** the original plan compared G1 with a simple breakout of the previous 12 hourly bars (no triangle and no 4-hour filter), with the same risk, costs, 24-hour limit and cooldown, over 180 calendar days of forward data. That test was never run.

## Reproduce

There is no official evaluator path: the team evaluator refuses G1, because G1 needs hourly and 4-hourly context that the evaluator's data does not carry.

- Synthetic unit tests of the reference code (29 tests on fictional bars, no market data):

  ```
  py -3.12 -X utf8 -B -m unittest tests.test_ikigai_reference
  ```

- Indicative replay on history: the platform's replay script (`g1_reference_dev.py`) was an internal research file and is not in this repository. It ran the reference code `trading_lab/ikigai_g1_reference.py` on hourly bars built from a local BTC-USDT-SWAP 1-minute store and found 0 setups and 0 trades, with the funnel shown in the table above.
- The V2 follow-up came from the slot's own research script (`v2_pullback.py`), which is not in this repository either.
- Code hashes: the reference is sha256 `912015f147731f85907465d83b9cc680613ae32df589214ec58718d8deb25312` and the spec is sha256 `18ab6f5f2001d2300ce91650af31787f5c035c3f77ed340e11efcc35cc7718f8`, both as pinned in `trading_lab/ikigai_g1.py`.
