# NC1 SOL-K3-TK-T400-NOREG: deep capitulation on SOL, no regime filter

**Status:** Failed gate K

- **Author:** Opus (a fictional persona name), a Claude Opus 5.5 session in the Claude team era, as part of the NC1 search cycle.
- **Frozen:** 2026-11-17, committed together with the NC1 pre-registration before any evaluation run, as `SOL-K3-TK-T400-NOREG.json` (file sha256 `b16da88dd8a1460b45d86daade955479bfa677311675683c24c5cd97af3d3ba3`, recipe_sha256 from the official validator `4f1893f79ebed4884f5f7609552aad4ac7c3f37b014b73d568017f92f2a1ca01`).
- **Instrument:** OKX SOL-USDT-SWAP (USDT-margined perpetual).
- **Side:** long only.
- **Holding horizon:** up to 12 hours.

## Idea

When the price falls far below its high of the last four hours, measured in typical hourly moves, the drop is probably forced selling (liquidations, margin calls). Forced selling overshoots, so after the first sign of recovery part of the move should come back within hours, and deeper drops should bounce more. Earlier work by the same author pointed the same way, more strongly on SOL than on BTC, but without statistical proof. In NC1 every selectable candidate also required a "down" regime (price not above its 20-day average). This recipe is the pre-registered control twin without that filter ("NOREG"). It could not be selected in NC1, but NC1 showed that the filter did not help, so this unfiltered version is the most informative member of the family.

## Exact rules

- **Entry** (both conditions, closed bars only):
  - `dist_high_bps(5m, 48) <= -6.0 x realized_vol_bps(1h, 720)`: the last 1-minute close is at least 6 sigma below the highest high of the last 48 closed 5-minute bars (4 hours), where sigma is the standard deviation of the last 720 hourly log returns (30 days), in bps. In the NC1 grid this is depth k = 3 (threshold -2k sigma).
  - `return_bps(1m, 15) > 0`: the last 1-minute close is above the close 15 minutes earlier.
- **Order:** taker entry (`order.type: taker`).
- **Stop loss:** 500 bps. **Take profit:** 400 bps. **Max hold:** 43,200 s (12 h). **Cooldown:** 600 s.
- **Size:** 30 % of the budget, one position, no adds, no rule-based exit.
- **No regime filter.** The `original_text` in the JSON is the shared NC1 hypothesis and still mentions the 20-day-average condition; the executable `rules` do not contain it. Only `rules` run.

## Results

Budget 100 USDT per evaluation window, so one position is 30 USDT. Stress means +5 bps extra cost on every fill. Official evaluator v4 (execution model `v4-path5s-2`), information-only run on 2026-11-17 over the whole SOL DEV (OKX history up to 2026-10-02). It was not a formal gate submission.

| Period | Trades | Net, USDT | Stress, USDT | Notes |
|---|---:|---:|---:|---|
| SOL DEV 2021-01-23 to 2026-10-02 (whole DEV) | 444 | +22.98 | +9.83 | +17.3 bps per trade; win rate 57 %. Exits: 187 target, 133 stop, 124 max hold. Account drawdown about 33 % of capital |
| 2024-01-01 to 2026-10-02 (gate K epoch E2) | 171 | +2.18 | -2.83 | 6 of 11 active quarters positive (55 %) |
| Tail 2025-07-01 to 2026-10-02 (held-out part of DEV, first look) | 81 | -2.38 | -4.76 | -9.8 bps per trade. A control that buys SOL every day at 00:00–00:04 UTC with the same exits lost 14.8 bps per trade in the same tail (458 trades) |
| Net by calendar year | | | | 2021 +14.05, 2022 -10.31, 2023 +17.02, 2024 +8.81, 2025 -5.53, 2026 (to 2 October) -1.10 |

The E2 and yearly rows use the evaluator's conservative funding assumption (on the whole DEV it gives net +22.94 and stress +9.80); the other rows use realised funding.

- **Gate K: FAIL** (verdict of the official evaluator on this run). Failed: worst calendar year 2022 at -10.3 % of capital (limit -10 %); E2 stress below 0; E2 share of positive quarters 55 % (needs 60 %); tail net below 0. Passed: net and stress above 0 on the whole DEV, at least 100 trades, and net without the 10 best trades +5.79 USDT (the 400 bps target keeps single wins small).
- **Gate D:** not submitted. The team submitted instead the synthesis recipe SOL-NC1-K6-F0-EK for the owner's MYHuman slot (see its card, `myhuman-nc1-synthesis`). It uses this entry as one of its two branches, with the same exits, and its entries overlap this recipe's entries by 97.8 % in the team's clone test. That submission had no evaluated forward trades (verdict INSUFFICIENT) when the project stopped.
- **NC1 itself had no candidate.** All 16 filtered candidates failed the "net without the 10 best trades >= 0" check on 2020-01 to 2025-06. The unfiltered twins did better in all 4 pairs tested (for example SOL k3 with post-only entry and a 1000 bps target: +27.18 against +7.40 USDT). The filtered sibling of this recipe (SOL-K3-TK-T400) made 261 trades and +4.44 USDT on that window (stress -3.31, without the 10 best trades -12.72) and was not selected.

## Why it is not enough

- It fades in recent data: 2025 and 2026 are negative, and the held-out tail loses 9.8 bps per trade. Gate K's tail rule exists to stop exactly this.
- It beats a naive daily buy with the same exits in the tail (-9.8 against -14.8 bps per trade), but after costs it still loses money.
- Falling markets hurt: 2022 lost 10.3 % of capital. In the MYHuman synthesis recipe that contains this entry, capitulation buys in calm downtrends were the weakest regime (-120 bps per trade over 18 tail trades).
- It was a control in NC1, not a candidate, and drew attention only after the NC1 results were known. The data up to 2025-06 is therefore not an independent test; the tail is the only clean look, and it is negative.
- The drawdown is large: about 33 % of capital over the whole DEV with a 500 bps stop and a 30 % position.
- A related Opus line faded the same way: the bounce of SOL after sharp BTC drops fell from +37.2 bps gross per trade in 2024 to +5.6 bps in the 2025–2026 tail.

## If you want to continue

- Treat all data up to 2026-10-02 as already seen. Any new version needs a pre-registration and a test on data after that date.
- Pre-register a filter that skips calm downtrends (in the team's regime classifier: `sma_gap_bps(1d, 20) <= 0` and `realized_vol_bps(1h, 168) <= realized_vol_bps(1h, 720)`) and judge it only on new data.
- Keep the taker entry. In NC1, post-only entries missed 25–28 % of signals, the misses were the rebounds that did not come back to the limit, and taker beat post-only in 7 of 8 pairs.
- Keep a naive control in every report (a daily buy with the same exits); only the edge over it needs explaining.
- Before tuning anything, study why the effect faded after 2024: compare 2024 with 2025–2026 by drop depth, time to rebound and the regime at the signal.

## Reproduce

```
py -3.12 -X utf8 -B tools/team_eval.py run --recipe <this folder>/SOL-K3-TK-T400-NOREG.json --author opus --set dev --store <data>/okx/SOL-USDT-SWAP
```

Expected (evaluator v4, execution model `v4-path5s-2`, run on 2026-11-17): 444 trades; net +22.94 and stress +9.80 USDT under the gate's conservative funding assumption (+22.98 and +9.83 with realised funding); gate K FAIL. Add `--end 2025-06-30` to stop at the end of the NC1 selection window. The `<data>` store must hold the project's OKX SOL-USDT-SWAP 1-minute data; market data is not included in this repository.
