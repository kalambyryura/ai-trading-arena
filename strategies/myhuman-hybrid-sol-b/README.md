# SOL-B-S1500T3000 (team hybrid for the MYHuman slot, SOL version)

**Status:** Failed gate K

- **Author:** kalambyrYura (project owner, human) is the author of record; MYHuman was the owner's own seat in the five-bot Live arena. The selection, code and tests were done by the slot's AI assistant session (Claude Opus 5.5). The only branch left after selection, B, was proposed by the AI author persona Sol (a Claude Opus 5.5 session). Astra's branch C, Opus's branch D and Sonnet's regime filter R were tested and dropped.
- **Frozen:** 2026-11-10.
- **Instrument:** SOL-USDT-SWAP (OKX linear perpetual).
- **Side:** long only, one position at a time.
- **Holding horizon:** up to 23.5 hours.
- **Decisions:** fixed rules only (recipe schema 4, closed 1-minute bars). Jev, the third-party model the project tried as an optional entry veto, plays no part in its decisions.

## Idea

This is the SOL version of the team hybrid ([ETH-RBCD-S1500T3000](../myhuman-hybrid-eth-rbcd/)), built on SOL data with a minimal grid: six branch sets and one exit. The pre-registered selection rule kept a single branch, Sol's B, because adding Astra's C, Opus's D or Sonnet's regime filter R made 2024–2025 worse. So the SOL version is not a multi-branch hybrid. It buys when the price comes back to 5–30 bps under its 7-day high after rising at least 0.30% in the last hour. It holds for up to 23.5 hours; the wide stop and target almost never trigger, so nearly all trades end on time. On 2026-11-10 the owner moved the Live seat from the ETH version to this one, from the next round on, knowing it had failed gate K.

## Exact rules

Enter long when **all** of these are true (checks on closed bars):

- `dist_high_bps(1d,7)` ≥ −30 and ≤ −5: the price is 5 to 30 bps below the highest high of the last 7 closed UTC days;
- `return_bps(1m,60)` ≥ 30: up at least 30 bps over the last 60 one-minute bars.

Exit and sizing:

- Order type: taker (market) entry.
- Stop loss: 1,500 bps (15%) below the entry.
- Take profit: 3,000 bps (30%) above the entry.
- Max hold: 84,600 s (23.5 hours).
- Cooldown after an exit: 900 s (15 minutes).
- Size: 30% of the 100 USDT account. In Live the team limited unproven recipes to 1 lot (0.01 SOL).
- A signal that arrives while a position is open is dropped. If an input is unknown (missing data), there is no entry.

## Results

All money figures are USDT on one 100 USDT account, from the official team evaluator (`tools/team_eval.py`, schema-4 candle engine, execution model `v4-path5s-2`). Costs are a 5 bps fee and 2 bps slippage per execution, a 1-tick spread, and funding, with a conservative charge where funding history is missing. "Stress" takes another 5 bps off every execution. The research data (DEV) ends on 2026-10-02 18:31 UTC; the SOL history starts on 2021-01-22.

| Period | Trades | Net, USDT | Stress, USDT | Notes |
|---|---:|---:|---:|---|
| Selection: 2021-01-22 → 2025-06-30 | 272 | +70.99 | +62.92 | Chosen from 6 branch sets. In 2024-01 → 2025-06: 92 trades, +28.9 bps per trade, 40.3 bps per trade better than the control "buy daily in an uptrend" |
| Whole DEV: 2021-01-22 → 2026-10-02 | 341 | +65.76 | +55.67 | Without the 10 best trades +10.98; worst year 2022 at −6.03%; account drawdown 18.1% of capital |
| E1, old epoch: 2021-01-22 → 2023-12-31 | 180 | +63.26 | +57.88 | Not evaluated: the SOL data cover only 73% of the epoch |
| E2, modern epoch: 2024-01-01 → 2026-10-02 | 161 | +2.51 | −2.21 | Positive in 5 of 11 quarters; fails both the stress check and the 60% quarter check |
| Tail: 2025-07-01 → 2026-10-02 (looked at once) | 69 | −5.23 | −7.25 | −26.8 bps per trade. Controls: "buy daily in an uptrend" −10.6 bps (231 trades); "buy daily" −14.6 bps (458 trades) |
| Live: three rounds of 24 hours, 11–17 Nov 2026 (UTC), 1 lot | 0 | 0 | — | 1,423, 1,042 and 1,422 entry checks, no signal. The second round was paused by a technical fault after 17.4 hours. A fourth round at 3× leverage was still running when this card was written (2026-11-17 UTC), with no signal in its first 35 minutes; its result is not included |

**Gate K: FAIL**, on three checks: E2 net and stress, the share of positive E2 quarters, and the tail. E1 is not evaluated on SOL.

**Gate D:** not submitted.

The controls buy at 00:00–00:04 UTC every day ("buy daily") or only when the 28-day return is positive ("buy daily in an uptrend"), with the same exits.

More detail:

- Net by year, USDT: 2021 +30.2 · 2022 −6.0 · 2023 +39.1 · 2024 +6.5 · 2025 −6.0 · 2026 +2.0. 2021 and 2023 together gave +69.29, more than the whole +65.76. The other four years together were −3.52.
- Exits: 332 on time, 7 at the stop, 2 at the target; 50.1% of trades were winners. The stop and the target barely matter: the result is decided by the 23.5-hour hold.
- In the tail the recipe was 16.2 bps per trade worse than "buy daily in an uptrend" and 12.2 bps worse than "buy daily". In 2024-01 → 2025-06 it had been the other way round (+28.9 bps against −11.4 and −10.1). The selection-period edge did not carry over.
- Why Live saw no signal: during the first two SOL rounds the price stayed 10.3–17.1% under its 7-day high. On DEV, when a 24-hour window starts at least 12.5% under the 7-day high, a signal appears in about 1.4% of windows (21% over all starts). Over the first two SOL rounds, DEV predicted 0.39 signals for a random start and about 0.02 for such a start.
- Safe leverage by the team's leverage method: 4× (6× without the 1.5× safety margin). The worst move against a position on DEV was 1,557 bps.
- Overlap with the team's other candidate, H10 on ETH: 5.9% of entries, daily PnL correlation 0.18. Distinct.

## Why it is not enough

- **It failed on held-out data:** −26.8 bps per trade in the tail, worse than both blind-buying controls.
- **The profit sits in two years:** 2021 and 2023 explain more than the whole DEV result.
- **The stop and the target are decoration:** 332 of 341 trades closed on time. In effect it is "buy near the 7-day high after a one-hour rise and hold for a day".
- **It almost never trades when the price is far below its recent high:** zero signals in three Live rounds. In the market seen in November 2026, reaching 20 trades for gate D would take about 1,400 daily rounds (about 95 on average).
- **The branch evidence was weak:** in Sol's own research the event had t = 0.44 in 2024 to 2025 Q1 (long and short together), and a placebo did no worse. Branch B on SOL had also been partly seen before the selection.
- **Long only:** part of the result is SOL's upward drift, which is why the controls exist.
- **Drawdown:** the account drawdown on DEV was 18.1% of capital, and gate D allows at most 10%.
- **Execution:** SOL's spread is sometimes two price ticks (about 2.25 bps at a price of about 89 USDT), above the executor's 2 bps limit, and then the entry is refused. The evaluator quotes one tick and does not see this. In Live it blocked 1 check out of 2,465. Because one tick is 100/price bps, the limit would block every SOL entry below a price of about 50 USDT.

## If you want to continue

- **Look at the NC1 synthesis first** ([SOL-NC1-K6-F0-EK](../myhuman-nc1-synthesis/)). It was built to replace this recipe, adds a capitulation branch and a 20-day trend filter, trades more often, and 81.5% of its entries overlap this one's.
- **Pre-register a variant whose exits actually work**, for example a shorter hold or a closer target, and test it only on data after 2026-10-02.
- **Add a random-entry control** with the same 23.5-hour hold, to measure how much of the result is SOL's drift.
- **Revisit the executor's 2 bps spread limit for SOL** before any Live use; otherwise valid signals can be skipped.

## Reproduce

The recipe runs on the official evaluator. The frozen recipe file `SOL-B-S1500T3000.json` sits next to this README.

```
py -3.12 -X utf8 -B tools/team_eval.py run --recipe <this folder>/SOL-B-S1500T3000.json --store <SOL-USDT-SWAP data store> --out <output folder>
```

- Build the store of OKX 1-minute candles and funding with the repository's data downloader (see QUICKSTART). The evaluator stops DEV runs at 2026-10-02 18:31 UTC and needs about 3 GB of free RAM.
- Expected: 341 closed trades, net +65.76 USDT, stress +55.67 USDT, gate K FAIL on `E2_net_and_stress`, `E2_positive_active_quarter_share` and `E2_tail`, with `E1_stress_floor` not evaluated. These numbers are exact only for the team's SOL store (catalogue sha256 `15316b49f267907b2325e9ba8495c7894862819815cb40881b6e7639864144e4`). A fresh download can differ slightly.
- Hashes of the frozen file: file sha256 `deed98ad185d47811e52408a675ec253ee41df8ec2babf8a98755b2ae8042787`, `recipe_sha256` `f357d8d53426df03cf11795458d4054f734f6e8475a93a48a891fb90a38c4d70`. The `semantic_sha256` `c630cb55d7cf6a32d9d341f6116bb8df4c19c90315f2e3f5fd8b28b5ea16449a` covers only the instrument, the rules, the feature catalogue and the execution model, which are what the trade list depends on.
- The selection grid and the controls came from the slot's own scripts (`hybrid/sol/run_sol.py sel` and `final` in the slot folder). They read a local data store and are not in this repository.
