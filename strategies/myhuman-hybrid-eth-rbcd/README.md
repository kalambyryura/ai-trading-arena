# ETH-RBCD-S1500T3000 (team hybrid for the MYHuman slot, ETH version)

**Status:** Failed gate K

- **Author:** kalambyrYura (project owner, human) is the author of record; MYHuman was the owner's own seat in the five-bot Live arena. The integration, code and tests were done by the slot's AI assistant session (Claude Opus 5.5). The entry branches were proposed by the AI author personas Sol (branch B), Astra (branch C), Opus (branch D) and Sonnet (regime filter R), all Claude Opus 5.5 sessions.
- **Frozen:** 2026-11-08.
- **Instrument:** ETH-USDT-SWAP (OKX linear perpetual).
- **Side:** long only, one position at a time.
- **Holding horizon:** up to 23.5 hours.
- **Decisions:** fixed rules only (recipe schema 4, closed 1-minute bars). Jev, the third-party model the project tried as an optional entry veto, plays no part in its decisions.

## Idea

The owner wanted a five-bot arena on several coins and asked the whole team for a hybrid for the owner's own seat. Four AI authors each proposed a long entry from their own research, and the slot's assistant combined them with one shared exit. The entry fires on any of three branches:

- **B∧R:** Sol's "approach to the 7-day high after a one-hour rise", used only when the price is above its 20-day average (Sonnet's regime filter);
- **C:** Astra's "buy in the first five minutes of a UTC day after a down day, inside a positive 28-day trend";
- **D:** Opus's "capitulation bounce": the price is at least 8 typical hourly moves under its 4-hour high and has been rising for 15 minutes.

The exits are wide (a 15% stop, a 30% target and a 23.5-hour limit); on DEV only 18 of 943 trades hit the stop. The variant was chosen from 12 pre-registered ones by a fixed rule, then checked once on held-out data. The owner put it into the Live arena on 2026-11-08 with the label "gate K FAIL", and two days later decided to replace it with the SOL version.

## Exact rules

Enter long when **any** branch is true (all checks on closed bars):

- **Branch B∧R (Sol + Sonnet)**, all of:
  - `dist_high_bps(1d,7)` ≥ −30 and ≤ −5: the price is 5 to 30 bps below the highest high of the last 7 closed UTC days;
  - `return_bps(1m,60)` ≥ 30: up at least 30 bps over the last 60 one-minute bars;
  - `sma_gap_bps(1d,20)` > 0: the price is above the mean close of the last 20 closed days.
- **Branch C (Astra)**, all of:
  - `return_bps(1d,28)` > 0: the last daily close is above the close 28 days earlier;
  - `return_bps(1d,1)` < 0: the last day closed lower than the day before;
  - `utc_minute_of_day` ≤ 4: the time is 00:00–00:04 UTC.
- **Branch D (Opus)**, all of:
  - `dist_high_bps(5m,48)` ≤ −8.0 × `realized_vol_bps(1h,720)`: the price is below the highest high of the last 48 five-minute bars (4 hours) by at least 8 standard deviations of hourly log returns over the last 720 hours (30 days);
  - `return_bps(1m,15)` > 0: up over the last 15 minutes.

Exit and sizing:

- Order type: taker (market) entry.
- Stop loss: 1,500 bps (15%) below the entry.
- Take profit: 3,000 bps (30%) above the entry.
- Max hold: 84,600 s (23.5 hours).
- Cooldown after an exit: 900 s (15 minutes).
- Size: 30% of the 100 USDT account. In Live the team limited unproven recipes to 1 lot (0.001 ETH).
- A signal that arrives while a position is open is dropped.
- If any input of any branch is unknown (missing data), the whole entry is blocked.

## Results

All money figures are USDT on one 100 USDT account, from the official team evaluator (`tools/team_eval.py`, schema-4 candle engine, execution model `v4-path5s-2`). Costs are a 5 bps fee and 2 bps slippage per execution, a 1-tick spread, and funding, with a conservative charge where funding history is missing. "Stress" takes another 5 bps off every execution. The research data (DEV) ends on 2026-10-02 18:31 UTC.

| Period | Trades | Net, USDT | Stress, USDT | Notes |
|---|---:|---:|---:|---|
| Selection: 2020-01 → 2025-06-30 | 773 | +88.91 | +66.46 | Chosen from 12 configurations: 3 branch sets × 2 stop/target pairs × ETH or BTC. In 2024-01 → 2025-06: 190 trades, +13.4 bps per trade, 15.5 bps per trade better than the control "buy daily in an uptrend" |
| Whole DEV: 2020-01 → 2026-10-02 | 943 | +86.92 | +59.61 | Without the 10 best trades +21.27; worst year 2022 at −6.97%; account drawdown 33.8% of capital |
| E1, old epoch: 2020-01-01 → 2023-12-31 | — | — | +64.1 | Reported as +64.1% of capital; passes (at least −5% is required) |
| E2, modern epoch: 2024-01-01 → 2026-10-02 | 360 | +5.79 | −4.50 | Positive in 6 of 11 quarters; fails both the stress check and the 60% quarter check |
| Tail: 2025-07-01 → 2026-10-02 (looked at once) | 170 | −1.99 | −6.85 | −4.0 bps per trade. Controls: "buy daily in an uptrend" +3.9 bps (253 trades); "buy daily" −8.8 bps (458 trades) |
| Live: two 24-hour rounds, 8–11 Nov 2026, 1 lot | 1 | +0.0089 | — | One branch-D trade, closed on time: net +42.1 bps. The second round made 926 checks without a signal before it was paused |

**Gate K: FAIL**, on three checks: E2 net and stress, the share of positive E2 quarters, and the tail.

**Gate D:** not submitted.

The controls buy at 00:00–00:04 UTC every day ("buy daily") or only when the 28-day return is positive ("buy daily in an uptrend"), with the same exits.

More detail:

- Net by year, USDT: 2020 +43.3 · 2021 +32.6 · 2022 −7.0 · 2023 +12.2 · 2024 +6.7 · 2025 +5.6 · 2026 −6.6. Almost all the profit is from 2020–2021: +75.9 of +86.9 USDT.
- By branch (whole DEV): B∧R made 333 trades, +48.6 USDT (+48.9 bps per trade); C made 505 trades, +40.7 USDT (+27.1 bps); D made 105 trades, −0.2 USDT (−2.4 bps). Branch C signalled on 578 days and entered on 505; 73 signals were lost because a position was already open.
- Each branch alone in the tail, with the same exit (an observation after the look; the recipe was not changed): B∧R 57 trades, +29.5 bps per trade; C 104 trades, +14.8 bps; D 28 trades, −90.2 bps.
- The Live trade: 1 lot, 0.001 ETH (2.12 USDT). Gross +51.2 bps, fees −10.0 bps, funding +0.9 bps, net +42.1 bps (+0.0089 USDT), all matched to the exchange bills. One trade proves nothing.
- Safe leverage by the team's leverage method: 3× (5× without the 1.5× safety margin). The worst move against a position on DEV was 1,858 bps.
- Overlap with the team's other candidate, H10 on ETH: 12.6% of entries, daily PnL correlation 0.32. Distinct.

## Why it is not enough

- **The profit is old:** 2020–2021 gave +75.9 of the +86.9 USDT. The modern epoch is negative under stress, and 2026 (to 2 October) is −6.6 USDT.
- **It failed on held-out data:** −4.0 bps per trade in the tail, worse than simply buying every day in an uptrend (+3.9 bps).
- **Drawdowns are large:** the account drawdown on DEV was 33.8% of capital, and gate D allows at most 10%. Under stress costs the worst year, 2022, is −10.26 USDT.
- **Long only:** part of the result is ETH's upward drift, which is why the controls exist.
- **The branch evidence was weak to begin with:** each author's own evidence had t < 2 or was chosen after looking at results. The selection from 12 variants on partly seen history inflates the stage-1 numbers.
- **Branch D lost money in the tail** (−90.2 bps per trade on 28 trades). It was researched with a 4-hour hold but runs here for 23.5 hours.
- **One gap blocks everything.** Branch D needs 30 days of hourly data, so one missing minute can switch off all branches for up to 30 days.
- **Branch C has one five-minute window a day.** The ETH feature warm-up takes 47–48 minutes, so a round that starts between about 23:15 and 00:05 UTC loses branch C for its whole duration.
- **Too few trades for a forward verdict:** reaching 20 trades for gate D would take about 45 daily Live rounds on average, and about 125 in the market seen in November 2026 (price 12.9–16.9% under its 7-day high, 28-day return −10.6% to −12.8%).

## If you want to continue

- **Pre-register (B∧R) or C without branch D**, and test it only on data after 2026-10-02. It looked better in the tail, but that is an observation made after the look.
- **Give branch D its researched 4-hour hold** in a separate, pre-registered variant.
- **Add a random-entry control** with the same holding times, to separate timing from drift.
- **Compare with the SOL version** ([SOL-B-S1500T3000](../myhuman-hybrid-sol-b/)) and with the later NC1 synthesis ([SOL-NC1-K6-F0-EK](../myhuman-nc1-synthesis/)). The NC1 synthesis reuses branch B∧R and a −6σ variant of branch D with a 12-hour hold.

## Reproduce

The recipe runs on the official evaluator. The frozen recipe file `ETH-RBCD-S1500T3000.json` sits next to this README.

```
py -3.12 -X utf8 -B tools/team_eval.py run --recipe <this folder>/ETH-RBCD-S1500T3000.json --store <ETH-USDT-SWAP data store> --out <output folder>
```

- Build the store of OKX 1-minute candles and funding with the repository's data downloader (see QUICKSTART). The evaluator stops DEV runs at 2026-10-02 18:31 UTC and needs about 3 GB of free RAM.
- Expected: 943 closed trades, net +86.92 USDT, stress +59.61 USDT, gate K FAIL on `E2_net_and_stress`, `E2_positive_active_quarter_share` and `E2_tail`. These numbers are exact only for the team's ETH store (catalogue sha256 starting `b6b19dc6`). A fresh download can differ slightly.
- Hashes of the frozen file: file sha256 `44bb6901c74b9d999e93dcefeae17f3c6561927d3e590572bda0ed883713396d`, `recipe_sha256` `6fced0d4a601f4c4ead1b1ac3d9fdbc5588534a3da51ff599817e5c06572a54c`. The `semantic_sha256` `b53e6c54864efc40f57ccaf038061f19c4d3ead94c981a44572eaad4af758c5a` covers only the instrument, the rules, the feature catalogue and the execution model, which are what the trade list depends on.
- The selection grid, the controls and the single-branch runs came from the slot's own scripts (`hybrid/run_grid.py sel` and `final` in the slot folder). They read a local data store and are not in this repository.
