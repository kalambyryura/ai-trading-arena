# H10-ETH: upward burst in an uptrend (post-only entry)

**Status:** Gate K candidate, forward test incomplete

- **Author:** "Sonnet", a fictional name of an AI author session; model Claude Opus 5.5 (`claude-opus-5-5`).
- **Date frozen:** 2026-10-28.
- **Instrument:** OKX ETH-USDT-SWAP (USDT-margined perpetual).
- **Side:** long only.
- **Holding horizon:** up to 24 hours (stop -5 %, target +10 %).

This is the only strategy of the project that passed gate K (the development-data gate). It passed with a thin margin, and the forward test (gate D) never got past 1 of its 30 declared trades. It is not proven and it is not called profitable.

## Idea

When ETH rises sharply within one hour (at least 3 times its typical hourly move over the last 7 days) while the price is above its 20-day average, the move tends to continue for about a day: fear of missing out, short covering and late trend buyers. Sharp falls did not behave this way in the author's tests, so the strategy is long only. Instead of chasing the move, it places a passive (post-only) buy a quarter of a typical hourly move below the signal price and lets it wait up to 2 hours. If the order is not filled, there is no trade. About 0.2 trades per day.

The configuration was chosen on BTC (4 maker-entry variants) and moved to ETH without changing any parameter. A blind replication on ETH 2020-01 to 2025-06 passed (462 trades, +61.5 bps net per trade, t 3.10, 16 of 22 quarters positive, author's simulator), and only then was the ETH tail looked at once. Another author in the team (Opus) independently found the same burst effect on BTC.

## Exact rules

Copied from `recipe.json` (strategy schema 4). Features use closed bars only; a feature without enough history is UNKNOWN and blocks entry.

| Field | Value |
|---|---|
| Instrument, side | `ETH-USDT-SWAP`, `long` |
| Entry, all of | `return_bps(1m, 60) >= 3.0 x rms_bar_bps(1h, 168)` and `sma_gap_bps(1d, 20) > 0` |
| Features | `return_bps(1m, 60)`: move over the last 60 minutes, bps. `rms_bar_bps(1h, 168)`: root mean square of the move inside each hour (close vs open of the same hour) over the last 168 closed hours, bps. `sma_gap_bps(1d, 20)`: distance of the price from the average of the last 20 closed daily closes, bps. |
| Order | `post_only` limit buy, offset `0.25 x rms_bar_bps(1h, 168)` below the signal close, `timeout_seconds` 7200; an unfilled order is cancelled with no trade |
| Stop loss | `stop_loss_bps` 500 |
| Take profit | `take_profit_bps` 1000 |
| Max hold | `max_hold_seconds` 86400 |
| Cooldown | `cooldown_seconds` 600 after an exit |
| Size | `size_pct` 11 of a 100 USDT allocation, rounded down to the exchange lot |

How the official evaluator executes it (execution model `v4-path5s-2`):

- A bar becomes visible 5 s after it closes; the order reaches the book one quote (+5 s) later, after the entry is checked again. A post-only order that would cross the market is rejected.
- A maker fill counts only when the modelled price path goes through the limit by at least max(1 bps, 2 ticks) before the timeout; a touch is not a fill. Maker cost: 2 bps fee + 2 bps adverse selection.
- Exits are taker: 5 bps fee + 2 bps slippage. The stop is live from the fill; the target from the next minute; if both are reachable in one minute, the stop wins.
- Stress test: +5 bps on every execution. Funding: real OKX funding where the data has it, a conservative charge for 8-hour slots without data.

## Results

All money figures are USDT on one 100 USDT account with one position at a time, after costs. DEV is the development data, 2020-01-01 to 2026-10-02 18:31 UTC. E2 is the modern epoch from 2024-01-01; the E2 tail is its last 5 quarters.

| Period | Trades | Net | Stress | Notes |
|---|---|---|---|---|
| Whole DEV, 2020-01-01 to 2026-10-02 | 550 | +30.81 | +25.33 | Official evaluator. 639 orders. Exits: 431 by time, 34 at target, 85 at stop. Worst year 2025: +0.53 % of capital. Without the 10 best trades: +19.81. Largest account drawdown 5.89 %. |
| E1, 2020-01-01 to 2023-12-31 | not reported | not reported | +20.58 | Gate: stress not worse than -5 % of capital. |
| E2, 2024-01-01 to 2026-10-02 | 200 | +6.68 | +4.75 | 11 active quarters, 7 positive (63.6 %; the gate needs 60 %). |
| E2 tail, 2025-07-01 to 2026-10-02 | 93 | +6.03 | +5.13 | Gate: at least 15 trades and net above 0. |
| Whole DEV, floor case: fills only where the price went 2 bps through the limit, each entry charged as a taker at 5 + 2 bps | 546 | +28.53 | about +22.6 (author's estimate) | Lower than every cell of the maker sensitivity grid (+29.64 to +31.29). |
| Gate D sample (sealed and forward data), 2026-10-02 18:32 to 2026-10-30 08:00 UTC | 1 | -0.081 | -0.092 | Run by the Judge (the team's independent evaluator role). 1 signal, 1 order, 1 fill. |
| Live, 7 rounds of about 24 h each, 2026-11-02 to 2026-11-17 | 0 | 0 | not applicable | Real money, 1-lot test size, rounds started by the project owner. 0 signals and 0 orders in every round. |

Other checks reported by the author:

- Funding scenarios on the whole DEV: base +31.29, conservative (the verdict) +30.81, sensitivity +29.84. Funding was -2.2 % of net.
- Stop slippage stress on the 85 stop exits: -2.30, leaving +28.99.
- 75 maker orders were not filled and 14 were rejected as crossing.
- 14 of 16 neighbouring configurations also passed the gate K numbers, and the tail was positive in all 16 (author's simulator).
- In the five Live rounds the author analysed (6,152 decision minutes, about 4.27 days), the trend filter was on in only 6.0 % of minutes, against 51.8 % on DEV. While the filter was on, the burst never went above 0.64 of the required 3. On DEV, 40.2 % of 102.5-hour windows also had no order, so zero trades was not unusual.
- An eighth Live round (3x leverage, started 2026-11-17 13:56 UTC) was still running when this card was written. Its result is not included.

**Gate K: PASS** (2026-10-29). The author committed the decision rule before the run. The platform had already published the same numbers, so the rule fixed the decision, not a prediction. The author's official run, the platform's run and the coordinator's reproduction gave the same trade list byte for byte. Later code versions were re-checked and gave the same trade list. The margin is thin (see below).

**Gate D: INSUFFICIENT.** 1 closed trade of the declared 30. This is the last recorded verdict (the Judge's check of 2026-11-08). Monthly re-evaluations were cancelled on 2026-11-17, when trading was wound down. The Live part of gate D needs 2 rounds of 24 h or more with at least one fill each; it reached 0 of 2.

## Why it is not enough

- **Thin margin.** E2 passes with 7 positive quarters out of 11 (63.6 % against 60 %). If one of them flipped, for example 2024Q2 (+0.38 on 9 trades), E2 would fail (6 of 11 = 54.5 %).
- **Small return.** E2 net +6.68 over 2.75 years is about 2.4 % per year on the 100 USDT allocation.
- **Rare trades.** About 0.2 trades per day. At the DEV pace, the 30th forward trade was expected around 2027-03-10 to 2027-04-08 (median range), and the Live period had almost no trend-filter time.
- **Few large winners carry it.** Positive skew: the profit depends on rare take-profits. By the author's own count, the tail without 2025Q3 was +34 bps per trade (t 0.98), and without its 10 best trades about 0.
- **Weak out-of-sample evidence.** Only the tail was out of sample (t about 2.3). It was not fully blind: the author model had general market knowledge up to mid-2026.
- **Depends on volatility.** The same configuration on BTC failed its tail when BTC's volatility fell (24-hour sigma about 179 bps; the gross time exit was +12 bps, below costs of about 14 bps). ETH kept a sigma of about 308 bps. If ETH calms down, the effect may vanish the same way.
- **The maker entry is a model.** Real post-only fills were never measured in Live (0 fills), and the team's rules required 2 Live rounds with fills before trusting them.
- **30 trades decide little.** The same trade distribution shifted to zero mean passes the gate D numbers in about 43 % of cases (likelihood ratio about 1.5). A PASS on 30 trades would have been weak evidence, and so would a FAIL.
- **Funding before 2022 is modelled.** It is a conservative charge, not real history: before 2021-08-31 16:00 UTC and in 325 missing settlements.

## If you want to continue

- **Keep the recipe frozen and count new trades.** Count the first 30 closed trades from 2026-10-02 18:32 UTC (the first bar after the DEV cutoff) with the same evaluator and the gate D rules (see [docs/METHODOLOGY.md](../../docs/METHODOLOGY.md), sections 6 and 10.4). The candles are public, so anyone can extend the sample.
- **Add instruments to collect trades faster.** The H12 design (pre-registered, never evaluated as one bot) trades the same rule on ETH and SOL, one slot each: about 0.41 trades per day on the DEV tail. SOL alone is weaker: the maker version passed the gate K numbers in the author's simulator, but with t 1.40, and the taker version failed the SOL tail.
- **Pair it with a down-regime strategy.** H10 waits whenever ETH is below its 20-day average. The author's attempt at this (NC1, short on continued decline) found nothing; see `sonnet-nc1-short-continuation`.
- **Measure maker fills for real.** Use the smallest size and record fill share, price move after the fill, and rejected post-only orders before trusting the maker model.
- **Watch volatility.** Track ETH's daily volatility against the fixed costs. The BTC failure suggests the edge disappears when moves shrink.

## Reproduce

The recipe and the official evaluator are in this repository. Market data is not: you download it from OKX's public API (no keys needed).

1. Check the recipe offline:

   ```
   py -3.12 -X utf8 -B tools/strategy_workshop.py validate strategies/sonnet-h10-eth/recipe.json
   ```

   Expected `recipe_sha256`: `dcf486662716f1b7a986c0e9f2f0d50d93d6fba8ad928d81f73541e77f906a81`. The sha256 of the file `recipe.json` itself: `504be5db96823f6228cb989d8518a32c4c2d736151483d358f0db51758d9b92f`.

2. Build an ETH-USDT-SWAP data store (details in the repository QUICKSTART):
   - 1-minute candles from 2020-01-01 00:00 to 2026-10-02 18:31 UTC, the DEV cutoff. The team's copy had 3,552,152 minutes and no gaps. Fetch with `tools/public_history.py fetch --inst ETH-USDT-SWAP --start 2020-01-01 --cutoff 2026-10-02T18:32:35.865Z --out <store>/ETH-USDT-SWAP/<folder>`, which uses public GET requests only.
   - Funding: the OKX API returns only about the last three months. The team also added OKX's official monthly funding archive (2021-09, then 2022-01 onward). Without the archive, the evaluator charges every uncovered 8-hour slot against the position, so net is a little lower but the trades do not change (H10 reads candles only). The earlier official run, with funding only from 2026-07-27, gave +30.57 net and +25.09 stress on the same trades.
   - Catalogue the store: `py -3.12 -X utf8 -B tools/team_data.py catalog --root <store>/ETH-USDT-SWAP --inst ETH-USDT-SWAP`.

3. Run the official evaluator on the whole DEV:

   ```
   py -3.12 -X utf8 -B tools/team_eval.py run --recipe strategies/sonnet-h10-eth/recipe.json --author you --label H10-ETH-repro --set dev --store <store>/ETH-USDT-SWAP --out runs/h10_dev
   ```

   It is one process: about 2-3 minutes and under 1.5 GB of memory. It refuses to start with less than 3 GB of free RAM.

4. Compare with the recorded result. It came from the team's data store and the evaluator files `tools/team_eval.py` (sha256 `b377f01a...`) and `tools/team_eval_v4.py` (sha256 `c6a440c4...`), with execution model `v4-path5s-2`. If the public copies of these files differ, for example in default paths, compare the numbers rather than the file hashes. Expected:
   - 550 closed trades, 639 orders;
   - net +30.808086, stress +25.329951;
   - trade list sha256 `3f26c2e7ee0fda5469e413fd35f9bed0cd1c50d920ecb8d97a0e36647777cac9`.

   Without `--prereg` and `--trials`, the overall verdict reads `NOT_ALL_CHECKS_EVALUATED`. That is expected: only the paperwork check is skipped, and every numeric gate K check shows PASS. The author's own run passed `--prereg` and `--trials` (a decision rule committed before the run, plus the full trial log) and got PASS.

The gate D sample (sealed and forward data after the DEV cutoff) was held by the Judge and is not in this repository. The same public candles can be fetched from OKX.
