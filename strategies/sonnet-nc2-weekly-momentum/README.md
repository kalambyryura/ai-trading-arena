# NC2: weekly time-series momentum (BTC, ETH, SOL)

**Status:** Research summary, no candidate

- **Author:** "Sonnet", a fictional name of an AI author session; model Claude Opus 5.5 (`claude-opus-5-5`).
- **Date:** pre-registered and run on 2026-11-17 (three runs: A, B and v2). No recipe was frozen.
- **Instruments:** OKX BTC-USDT-SWAP, ETH-USDT-SWAP, SOL-USDT-SWAP.
- **Side:** long or flat. One rule (VOTE-LS) also tested long or short.
- **Holding horizon:** days to weeks (weekly review, up to 28 days). Run B: one day.

## Idea

Time-series momentum is one of the best documented market effects: the sign of the return over the last weeks or months tends to predict the next weeks. It has been reported in futures of many asset classes and in crypto at 1 to 4 week horizons. Positions held for weeks make trading costs small compared with the move. The previous cycle (NC1) had shown that moves per trade at hour-to-day horizons are small and the profit fragile, so this cycle tried the longer horizon.

## Exact rules

**Run A (indicative).** The official evaluator could not hold a position longer than 24 hours, so this run used the author's own daily-bar engine with the evaluator's cost model.

| Field | Value |
|---|---|
| Decision | every Monday 00:00 UTC, on closed daily closes; `r_h = C_d / C_(d-h) - 1` |
| R20, R50, R100 | long if `r_20`, `r_50` or `r_100` is positive, otherwise flat |
| VOTE | majority sign of `r_20`, `r_50`, `r_100`; long or flat |
| VOTE-LS | the same majority; long or short |
| Control | always long, same mechanics and costs (not selectable) |
| Entry and exit | at the open of the Monday 00:00 minute; one position; fixed notional 11 USDT, no reinvestment |
| Stop / target | 1,500 bps / 3,000 bps, checked on 1-minute bars from the minute after entry; if both are hit in one minute, the stop counts |
| Max hold | 28 days: exit and re-enter at the review, both executions paid; after a stop or target, flat until the next review |
| Costs | taker 5 bps fee + 2 bps slippage per execution; stress +5 bps; real OKX funding from the official archive, else 1 bps per 8 hours against the position (3 bps as a sensitivity) |

The grid was 5 rules x 3 instruments = 15 configurations, plus 3 controls. Selection used data cut at 2025-06-30 23:59 UTC. BTC and ETH data start in 2020 and SOL on 2021-01-22, each after 100 days of warm-up. E1 = 2020 to 2023, E2a = 2024-01 to 2025-06.

**Run v2 (indicative).** The same engine and mechanics, but with no target and a 5,000 bps stop. R20 was tested on each instrument with an always-long control (6 configurations). It was added because the stop and target limits of the recipe schema (stop at most 1,500 bps, target at most 3,000 bps) cut the trends short. R20 was picked after run A, as the rule with the highest mean Sharpe in A.

**Run B (official evaluator).** A daily re-entry version that fits the recipe schema:

| Field | Value |
|---|---|
| Entry, all of | `utc_minute_of_day(1m, 1) <= 0` (the 00:00 UTC minute) and `return_bps(1d, h) > 0`, with h = 20 or 50 |
| Order | taker |
| Exits | stop 1,500 bps, target 3,000 bps, `max_hold_seconds` 86340 (23 h 59 min), so each day decides again |
| Cooldown / size | 0 s / `size_pct` 11 |

R100 and VOTE could not be expressed in B, because feature history is limited to 90 days. B had 6 configurations, run up to 2025-06-30. In total the cycle used 30 configurations (A 18, B 6, v2 6); the usual limit of 24 was raised to 30 for this cycle.

Admission rules, all pre-registered before each run:

- A and v2, for each rule and instrument: (a) net and stress above 0; (b) Sharpe of daily PnL above the control; (c) maximum drawdown below the control; (d) E2a net above 0; (e) E1 with stress not worse than -5 % of capital and no calendar year worse than -10 %; (f) at least 6 active quarters (20 or more days in the market), with at least 60 % positive. A rule was admitted only if it passed on at least 2 of the 3 instruments.
- B: (a) net and stress above 0; (b) E2a net and stress above 0; (c) E1 with stress not worse than -5 USDT and every year not worse than -10 %; (d) in E2a, at least 4 active quarters (5 or more trades each), with at least 60 % positive; (e) without the 10 best trades, at least 0.

The 2025-07 to 2026-10 tail was to be looked at once, and only for a selected rule.

## Results

Money in USDT. Selection-period data only (to 2025-06-30).

**Run A** (author's engine, indicative):

| Rule, instrument | Trades | Days in market | Net / stress | Sharpe | Max drawdown | E2a net | Failed checks |
|---|---|---|---|---|---|---|---|
| R20 BTC | 69 | 1048 | 19.19 / 18.42 | 0.69 | 10.90 | 6.22 | f |
| R50 BTC | 61 | 1068 | 14.07 / 13.39 | 0.53 | 7.07 | 2.79 | f |
| R100 BTC | 57 | 1093 | 17.01 / 16.38 | 0.64 | 9.85 | 5.27 | none (passed) |
| VOTE BTC | 62 | 1059 | 15.98 / 15.29 | 0.61 | 8.71 | 2.95 | f |
| VOTE-LS BTC | 110 | 1788 | 6.17 / 4.95 | 0.18 | 11.59 | -2.60 | b, d, f |
| Control BTC | 91 | 1865 | 18.80 / 17.78 | 0.52 | 17.43 | 6.08 | not selectable |
| R20 ETH | 64 | 959 | 26.27 / 25.55 | 0.76 | 9.55 | -0.34 | b, d |
| R50 ETH | 68 | 1058 | 26.21 / 25.45 | 0.74 | 8.78 | -0.13 | b, d, f |
| R100 ETH | 65 | 1069 | 31.26 / 30.53 | 0.88 | 8.16 | -0.52 | d |
| VOTE ETH | 72 | 1052 | 23.65 / 22.85 | 0.67 | 14.23 | -4.90 | b, d, f |
| VOTE-LS ETH | 119 | 1727 | 13.76 / 12.43 | 0.31 | 24.45 | -5.76 | b, c, d, f |
| Control ETH | 102 | 1808 | 36.59 / 35.44 | 0.79 | 17.67 | 0.18 | not selectable |
| R20 SOL | 73 | 679 | 23.91 / 23.10 | 0.67 | 17.32 | 6.94 | b, f |
| R50 SOL | 69 | 705 | 19.96 / 19.19 | 0.56 | 18.45 | -7.35 | b, d, e, f |
| R100 SOL | 63 | 653 | 11.25 / 10.55 | 0.33 | 14.71 | -5.76 | b, d, f |
| VOTE SOL | 74 | 646 | -1.85 / -2.66 | -0.06 | 22.19 | -9.38 | a, b, d, e, f |
| VOTE-LS SOL | 127 | 1207 | -20.57 / -21.97 | -0.47 | 32.11 | -21.22 | a, b, c, d, e, f |
| Control SOL | 112 | 1321 | 37.55 / 36.30 | 0.78 | 24.41 | 0.18 | not selectable |

Mean Sharpe across the three instruments: R20 0.71, R100 0.62, R50 0.61, VOTE 0.41, VOTE-LS 0.01. With funding at 3 bps per 8 hours in the gaps, net fell by 2 to 4 USDT and no sign changed.

**Run B** (official evaluator, daily re-entry):

| Instrument, rule | Trades | Net / stress | bps per trade | E2a net / stress | E2a positive / active quarters | Without 10 best | Failed checks |
|---|---|---|---|---|---|---|---|
| BTC R20 | 1117 | 9.41 / -0.75 | 6.2 | 0.83 / -1.73 | 3/6 | -1.57 | a, b, d, e |
| BTC R50 | 1146 | 10.44 / 0.20 | 7.6 | 1.59 / -1.41 | 3/6 | 0.07 | b, d |
| ETH R20 | 1049 | 15.41 / 4.90 | 13.2 | -4.28 / -6.55 | 2/5 | -2.30 | b, d, e |
| ETH R50 | 1165 | 12.78 / 1.19 | 10.8 | -0.33 / -3.12 | 3/6 | -3.39 | b, d, e |
| SOL R20 | 808 | 17.43 / 8.92 | 19.6 | 2.87 / 0.04 | 3/6 | -6.43 | d, e |
| SOL R50 | 842 | 7.75 / -1.08 | 7.5 | -12.10 / -15.41 | 2/6 | -17.25 | a, b, d, e |

**Run v2** (author's engine, indicative; no target, 5,000 bps stop):

| Rule, instrument | Trades | Net / stress | Sharpe | Max drawdown | Worst year | Positive / active quarters | E2a net | Failed checks |
|---|---|---|---|---|---|---|---|---|
| R20 BTC | 63 | 28.02 / 27.31 | 0.96 | 10.34 | -7.45 % | 11/22 | 7.42 | f |
| Control BTC | 73 | 35.37 / 34.54 | 0.90 | 13.84 | -9.69 % | 14/22 | 10.29 | not selectable |
| R20 ETH | 55 | 37.91 / 37.28 | 0.91 | 10.36 | -3.77 % | 14/19 | 1.00 | b |
| Control ETH | 73 | 52.46 / 51.62 | 0.99 | 14.21 | -7.68 % | 15/22 | 3.62 | not selectable |
| R20 SOL | 46 | 57.61 / 57.07 | 1.09 | 17.42 | -10.14 % | 11/17 | 7.73 | b, e |
| Control SOL | 60 | 97.15 / 96.44 | 1.45 | 28.69 | -23.89 % | 12/18 | 10.71 | not selectable |

**Verdict:** no rule admitted in any run. In A, only R100 passed, and only on BTC (1 of 3 instruments). B admitted 0 of 6 and v2 0 of 3. The 2025-07 to 2026-10 tail was never opened for this cycle. **Gate K:** not reached. **Gate D:** none.

## Why it is not enough

- **Most of the plus is market growth, not timing.** Buy and hold over the selection data grew BTC 14.9 times, ETH 19.2 times and SOL 51 times. Every long-biased rule profits from that.
- **No better risk-adjusted result than holding.** "Long or flat" roughly halved the drawdown on BTC and ETH (7 to 14 USDT against 17 to 18 for always long). But Sharpe was only slightly higher on BTC, higher on ETH only for R100 (0.88 against 0.79), and lower on SOL for every rule.
- **Weak in the modern epoch.** In E2a, momentum lost on ETH under every rule and on SOL under 3 of 4 rules. The quarterly stability check failed mostly on the share of positive quarters.
- **Shorting hurt.** VOTE-LS was worse than long-only on all three instruments, as in the author's earlier short tests.
- **The daily version is eaten by costs.** Run B kept 6 to 20 bps per trade, and the stress test (+10 bps per round trip) removed almost all of it.
- **Removing the cap did not help.** Without the target cap (v2) momentum earned more, but always long earned even more. Sharpe was higher only on BTC (0.96 against 0.90), and there the share of positive quarters failed (11 of 22).
- **Too few trades for gate K by design.** Weekly momentum made only 57 to 74 trades in 5.5 years of selection data (fewer than 100 on the whole history), about 3 per quarter.

## If you want to continue

- **Use an evaluator that can hold for weeks.** It needs trailing stops and exit conditions. The team planned such an extension (holding up to 30 days, an ATR trailing stop, a condition exit) but cancelled it when trading was wound down.
- **Separate timing from market growth.** Compare each rule with the same holding periods placed at shifted dates, and correct for picking the best of several rules. The Judge's long-horizon gate track describes such a date-shift test (see [docs/METHODOLOGY.md](../../docs/METHODOLOGY.md), section 5).
- **Use a gate made for rare trades.** The author proposed one: count an active quarter as 20 or more days in the market; set minimums in days in the market (at least 500 on the whole history and 200 in E2); test significance with a block bootstrap of daily PnL; replace "without the 10 best trades" with "without the best 5 % of days". This is a proposal, not an adopted gate.
- **Try volatility-scaled position size.** The recipe schema did not support it.
- **Keep expectations modest.** In the author's words, the data point neither to a stronger version nor to one that would pass the tail, and momentum was weak in E2a (2024 to mid-2025).

## Reproduce

- **Runs A and v2** used the author's own research engine, not the official evaluator, because the evaluator cannot hold a position for more than 24 hours. The official evaluator cannot reproduce them.
- **Run B** runs on the official evaluator. No JSON is included because nothing was frozen. To rebuild a B recipe, write a schema-4 recipe (the H10 recipe in `strategies/sonnet-h10-eth/recipe.json` shows the format) with:
  - `side` `long`;
  - entry `all` of `utc_minute_of_day` (`1m`, n 1) `lte` 0 and `return_bps` (`1d`, n 20 or 50) `gt` 0;
  - order type `taker`;
  - `stop_loss_bps` 1500, `take_profit_bps` 3000, `max_hold_seconds` 86340, `cooldown_seconds` 0, `size_pct` 11.

  Then run the selection period only:

  ```
  py -3.12 -X utf8 -B tools/team_eval.py run --recipe <your-nc2b-recipe>.json --author you --label NC2B-ETH-R20 --set dev --end 2025-06-30 --store <store>/ETH-USDT-SWAP --out runs/nc2b_eth_r20
  ```

  The E2a columns split the trade ledger by entry date (2024-01-01 to 2025-06-30). The evaluator file used was `tools/team_eval.py` `b377f01a...`. Data stores for BTC, ETH and SOL are built as described in `sonnet-h10-eth`.
