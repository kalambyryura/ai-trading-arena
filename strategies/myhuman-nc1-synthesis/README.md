# SOL-NC1-K6-F0-EK (NC1 synthesis for the MYHuman slot)

**Status:** Not proven

- **Author:** kalambyrYura (project owner, human) is the author of record; MYHuman was the owner's own seat in the five-bot Live arena. The synthesis, code and tests were done by the slot's AI assistant session (Claude Opus 5.5) on the team lead's instruction. The two entry branches come from the AI author personas Sol (branch L) and Opus (branch K), both Claude Opus 5.5 sessions.
- **Frozen:** 2026-11-17, after a pre-registered selection on the same day.
- **Instrument:** SOL-USDT-SWAP (OKX linear perpetual).
- **Side:** long only, one position at a time.
- **Holding horizon:** up to 12 hours.
- **Decisions:** fixed rules only (recipe schema 4, closed 1-minute bars). Jev, the third-party model the project tried as an optional entry veto, plays no part in its decisions.

## Idea

The owner's slot needed a SOL recipe that trades more often than the previous one, [SOL-B-S1500T3000](../myhuman-hybrid-sol-b/). That recipe almost never signalled in Live: in the rounds reviewed in mid-November 2026, the price stayed at least 12.5% under its 7-day high. This synthesis joins two long entries that other authors found on SOL in the team's NC1 research cycle. Branch L (Sol) buys trend continuation: the price is above its 20-day average, just under its 7-day high, and up at least 0.30% in the last hour. Branch K (Opus) buys a capitulation bounce: the price has fallen far below its 4-hour high (six "typical hourly moves") and has been rising for 15 minutes. Either branch opens the position. The exits are short: a 5% stop, a 4% target or 12 hours. The label "not proven" was set before any test, because part of the history had already been seen by the component authors.

## Exact rules

Enter long when **either** branch is true (all checks on closed bars):

- **Branch L (Sol)**, all of:
  - `dist_high_bps(1d,7)` ≥ −30 and ≤ −5: the price is 5 to 30 bps below the highest high of the last 7 closed UTC days;
  - `return_bps(1m,60)` ≥ 30: up at least 30 bps over the last 60 one-minute bars;
  - `sma_gap_bps(1d,20)` > 0: the price is above the mean close of the last 20 closed days.
- **Branch K (Opus)**, all of:
  - `dist_high_bps(5m,48)` ≤ −6.0 × `realized_vol_bps(1h,720)`: the price is below the highest high of the last 48 five-minute bars (4 hours) by at least 6 standard deviations of hourly log returns over the last 720 hours (30 days);
  - `return_bps(1m,15)` > 0: up over the last 15 minutes.

Exit and sizing:

- Order type: taker (market) entry.
- Stop loss: 500 bps (5%) below the entry.
- Take profit: 400 bps (4%) above the entry.
- Max hold: 43,200 s (12 hours).
- Cooldown after an exit: 600 s (10 minutes).
- Size: 30% of the 100 USDT account. In Live the team limited unproven recipes to 1 lot (0.01 SOL).
- No market-regime filter (variant F0).
- If any input of any branch is unknown (missing data), the whole entry is blocked, not just that branch.

## Results

All money figures are USDT on one 100 USDT account, from the official team evaluator (`tools/team_eval.py`, schema-4 candle engine, execution model `v4-path5s-2`). Costs are a 5 bps fee and 2 bps slippage per execution, a 1-tick spread, and funding, with a conservative charge where funding history is missing. "Stress" takes another 5 bps off every execution. The research data (DEV) ends on 2026-10-02 18:31 UTC; the SOL history starts on 2021-01-22.

| Period | Trades | Net, USDT | Stress, USDT | Notes |
|---|---:|---:|---:|---|
| Selection: SOL data start → 2025-06-30 | 713 | +64.96 | +43.85 | Chosen by a pre-registered rule from 12 configurations (k = 6 or 8 for branch K, three regime-filter options, two exit sets); without the 10 best trades +47.37 |
| Whole DEV: 2021-01-22 → 2026-10-02 | 874 | +66.99 | +41.15 | Without the 10 best trades +49.40; worst year 2022 at −6.67% of capital; account drawdown 28.9% of capital |
| E2, modern epoch: 2024-01-01 → 2026-10-02 | 379 | +20.01 | +8.91 | Positive in 6 of 11 quarters; gate K needs 60% |
| Tail: 2025-07-01 → 2026-10-02 (looked at once) | 161 | +2.26 | −2.47 | +4.3 bps per trade. The control "buy every day at 00:00–00:04 UTC with the same exits": 458 trades, −14.8 bps per trade |
| Forward test (gate D sample from 2026-11-17 15:14 UTC) | 0 | — | — | Verdict INSUFFICIENT, 0 of the declared 30 trades; cancelled when the project wound down |
| Live | — | — | — | Never traded Live |

**Gate K: FAIL**, on one check only: positive E2 quarters were 6 of 11 (55%), and at least 60% is required. The other numeric checks passed. The old-epoch check (E1) is not evaluated on SOL, because its history starts in 2021. The Judge re-ran the recipe on the same data and got the same 874 trades and the same totals.

**Gate D: INSUFFICIENT.** The recipe was submitted with N = 30 in the same commit as the freeze. The Judge registered the submission on 2026-11-17 with 0 closed trades. No later evaluation took place.

More detail from the whole DEV run:

- Net by year, USDT: 2021 +25.0 · 2022 −6.7 · 2023 +28.7 · 2024 +18.8 · 2025 −4.2 · 2026 +5.4.
- Exits: 332 at the target, 353 on time, 189 at the stop.
- By branch: L made 441 trades, +45.97 USDT (+35.0 bps per trade); K made 433 trades, +21.22 USDT (+16.4 bps).
- By market regime at entry, in bps per trade (whole DEV / tail): UP_CALM +40.9 / +35.7; UP_STORM +40.6 / +0.7; DOWN_STORM +9.4 / +19.2; DOWN_CALM −33.1 / −120.1 (74 and 18 trades). UP or DOWN is the price against its 20-day average. CALM means the 7-day hourly volatility is not above the 30-day one; STORM means it is above.
- Frequency: when a 24-hour window starts at least 12.5% under the 7-day high (as all four Live rounds reviewed in mid-November did), the recipe signals in 18.3% of windows; the previous slot recipe signalled in 1.4%. Over all starts it is 29.5% against 21.4%.
- Safe leverage by the team's leverage method (a 1.5× margin to the worst adverse move, OKX tier 1): 6×. The worst move against a position on DEV was 1,071 bps.

### The tail, honestly

The tail (2025-07-01 → 2026-10-02) was opened once, after the selection. The recipe made 161 trades at +4.3 bps per trade after normal costs (+2.26 USDT). Under stress costs the result is −2.47 USDT. That passes gate K's tail check (at least 15 trades and net above zero) with almost no room. In the same period, buying SOL every day with the same exits lost 14.8 bps per trade. So the recipe lost less than blind buying in a weak market, which is not the same as making money. Its two parts behaved differently there. On their own, Sol's component made +14.2 bps per trade (70 trades) and Opus's component −9.8 bps (81 trades). The worst pocket was capitulation buying in calm falling markets (DOWN_CALM): −120.1 bps per trade on 18 trades. The tail was also not fully fresh: the author of branch L had already seen it.

### The two-window rule, and what actually happened

The team lead fixed the following rule on 2026-11-17, before any forward result existed:

1. A PASS on the first N = 30 forward trades gives "forward PASS, not proven". The recipe stays in Live at 1 lot, with no bigger size or leverage.
2. "Proven" only comes after a PASS in a second, independent window: the next 30 trades, the same gate D thresholds, and no overlap with the first window. The second window was declared before the first result.
3. A FAIL in either window takes the recipe out of Live, and the slot goes back to the owner's choice.
4. INSUFFICIENT means the sample keeps growing while Live rounds run.

What happened: the recipe was never moved into the Live slot. The owner had been asked whether to move it, and that question was withdrawn when trading stopped. On the same day, 2026-11-17, the project wound down: no new Live rounds, and the second window and the monthly re-evaluations were cancelled. The Judge's first evaluation had been planned for 2026-12-01. The recorded verdict stays INSUFFICIENT with 0 of 30 trades.

Even if the test had run, it would have been weak evidence. On DEV, windows of 30 consecutive trades pass the numeric gate D tests in 57.5% of all windows, 50.3% in E2 and only 28.0% in the tail. Those tests are: net above zero, stress above zero, the largest trade at most 30% of the gross profit, and a closed-trade drawdown of at most 10 USDT. At the DEV rate of 0.43 trades a day (0.35 in the tail), 30 trades take about 85 days.

## Why it is not enough

- **It failed gate K and the tail is flat:** +4.3 bps per trade before stress, −2.47 USDT after stress.
- **The history is not clean:** the configuration was picked from 12. The author of branch L had already seen the tail, and the K family was known up to 2025-06. The DEV numbers are therefore inflated.
- **Weak regime:** capitulation buys in calm falling markets lose (DOWN_CALM −33.1 bps on DEV, −120.1 bps in the tail).
- **Long only:** part of the DEV result is SOL's upward drift, which is why the daily-buy control exists. The best years were 2021 (+25.0 USDT) and 2023 (+28.7 USDT).
- **Drawdowns are large:** the account drawdown on DEV was 28.9% of capital, and gate D allows at most 10%. Under stress costs the worst year, 2022, is −12.74 USDT. That is beyond the −10% yearly limit, which the gate applies at normal costs.
- **It is not a new signal:** 97.8% of its entries overlap Opus's component and 80.3% overlap Sol's component (entries within 30 minutes). 81.5% overlap the previous slot recipe. It is distinct from the team's other candidate, H10 (8.0% overlap, daily PnL correlation 0.11).
- **Execution:** SOL's spread is sometimes two price ticks (about 2.4 bps), above the executor's 2 bps limit, and then the entry is refused. The evaluator does not model this. In two November Live rounds of the previous slot recipe it blocked 1 check out of 2,465.
- **No forward evidence:** 0 trades after the freeze.

## If you want to continue

- **Run the declared forward test.** Collect the first 30 closed trades after 2026-11-17 15:14:01 UTC as window 1 and the next 30 as window 2, with the gate D thresholds. The evaluator has `--set forward` and `--declared-n 30` for this (see [docs/METHODOLOGY.md](../../docs/METHODOLOGY.md), sections 6 and 10.4).
- **Treat DOWN_CALM as an after-the-fact idea.** A filter that skips branch K in calm falling markets must be pre-registered and tested only on data after 2026-10-02.
- **Add a random-entry control** with the same holding times, to separate timing from SOL's drift. In the team's long-trend cycle (NC2), most of the DEV profit of long positions turned out to be market growth.
- **Test the same rules unchanged on ETH or BTC**, pre-registered, as a transfer test.
- **Model the 2 bps spread limit** before any Live use on SOL.

## Reproduce

The recipe runs on the official evaluator. The frozen recipe file `SOL-NC1-K6-F0-EK.json` sits next to this README.

```
py -3.12 -X utf8 -B tools/team_eval.py run --recipe <this folder>/SOL-NC1-K6-F0-EK.json --store <SOL-USDT-SWAP data store> --out <output folder>
```

- Build the store of OKX 1-minute candles and funding with the repository's data downloader (see QUICKSTART). The evaluator stops DEV runs at 2026-10-02 18:31 UTC and needs about 3 GB of free RAM.
- Expected: 874 closed trades, net +66.99 USDT, stress +41.15 USDT, gate K FAIL on `E2_positive_active_quarter_share`. These numbers are exact only for the team's SOL store (catalogue sha256 `15316b49f267907b2325e9ba8495c7894862819815cb40881b6e7639864144e4`). A fresh download can differ slightly.
- Hashes of the frozen file: file sha256 `cecf75a58020e6f042a70678cb73e7af3c48209fe986adbff86c2600f37a6182`, `recipe_sha256` `e1f55979c09515109ab233f90fcb0ffe1fe955c0cf0cd44cb37e80fa01373114`. The `semantic_sha256` `e8550d07fca4f85b0cdeb237e1f6ba15285309f32da45a56324e588644bf8cc6` covers only the instrument, the rules, the feature catalogue and the execution model, which are what the trade list depends on.
- The selection grid, the controls and the branch and regime splits came from the slot's own scripts (`nc1/run_nc1.py sel` and `final` in the slot folder). They read a local data store and are not in this repository.
