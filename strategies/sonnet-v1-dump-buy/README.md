# Sonnet V1: "dump buy", 1 lot

**Status:** Not proven

- **Author:** the first "Sonnet" author session. Its recipe declares the model `claude-sonnet-5-5` (as declared, not verified). From 2026-10-28 the "Sonnet" persona was run on Claude Opus 5.5 and kept V1 only as a baseline.
- **Date frozen:** 2026-10-01 (22:40 UTC).
- **Instrument:** OKX BTC-USDT-SWAP.
- **Side:** long only.
- **Holding horizon:** up to 1 hour.

## Idea

Buy a sharp short drop. If BTC fell at least 0.35 % over the last 5 closed minutes, buy one lot and hold it for at most an hour, betting on a bounce. When the author froze it, they wrote that the entries beat random entries with the same exits by about +13 bps gross, which is about the cost of a round trip (about 14 bps: 2 x 5 bps fee + 2 x 2 bps slippage). So a net edge was not shown. The condition is true in only about 0.5 % of minutes (about 3.7 episodes per day), and the recipe waits in cash the rest of the time.

## Exact rules

Copied from `recipe.json` (the older V1/V2 recipe format of the arena).

| Field | Value |
|---|---|
| Entry, mode `all` | `return_5m_bps <= -35` on the last closed minute (single condition) |
| Order | taker; the engine checks every 30 s and enters at the next fresh quote after a re-check; full fill or skip |
| Spread filter | `max_spread_bps` 2 |
| Entries | `max_entries` 1 per cycle; `add_after_bps` 30 is present but unused |
| Stop loss | `stop_loss_bps` 100 |
| Take profit | `take_profit_bps` 200 |
| Max hold | `max_hold_seconds` 3600 |
| Formal exit | none (`exit` with an empty condition list) |
| Cooldown | `cooldown_seconds` 120 after a close |
| Size | `size_pct` 11 of a 100 USDT budget; the author describes this as exactly 1 lot (0.0001 BTC) for BTC prices of roughly 55,000 to 110,000 USDT |

The recipe text also lists what blocks an entry. Unknown data (fewer than 25 closed minutes, a gap in the candles, or a last candle older than 90 s) blocks it, and so does the arena limit of 20 entries per season. In the October 2026 arena, an optional AI veto could reject an allowed entry. That veto was "Jev", the third-party model `typesafe/jev-1.13`, reached through OpenRouter. The recipe's own rules do not use it, and the stop, target and time exit are run by code.

## Results

| Period | Trades | Net | Stress | Notes |
|---|---|---|---|---|
| Author's development data: 61 days to 2026-09-17, split into training and validation parts | 138 | about 0 bps per trade | not reported | Standard error about 4.7 bps. Chosen from about 920 tested configurations. On the development test stand, 62 of 82 windows had no trade. |
| 2024-01 to 2025-06 | not reported | -8.9 bps per trade | not reported | z -7.1. Fast simulator from the same author line's review of 2026-10-03, live-like costs. |
| 2025-07 to 2026-07-17 | 1,645 | -10.47 bps per trade | -13.56 bps per trade | z -6.08. 90 % interval -13.32 to -7.58. Three blocks of about 4 months: -10.3, -10.8, -10.1. |
| 2026-07-18 to 2026-10-02 | 176 | +2.8 bps per trade | not reported | z 0.6. Overlaps the data the recipe was chosen on. |
| Replay benchmark of the earlier platform (P1): 59 one-day windows | 135 | -0.100 USDT in total (-0.0017 USDT per window) | -0.0186 USDT per window at +5 bps | One recipe of a 944-run cohort. |
| Whole development history, 2020-01-01 to 2026-10-02, official exact engine | 16,966 | -198.30 USDT | not reported | Run on 2026-10-29 as a code-release identity test, not as a gate submission. No AI veto. |
| PAPER, 5 h, 2026-10-26 | 2 | +0.094 USDT | not applicable | One market window (+0.41 %). Its entry came 64 s apart from another bot's: in effect one event. |
| Live, 5 h, 2026-10-02 | 0 | 0 | not applicable | The AI veto was asked 5 times. Each time it answered "proceed" with confidence 0.60 to 0.65, below its 0.70 threshold, so there was no entry. |
| Live, 2026-10-30 16:23 to 21:24 UTC | 0 | 0 | not applicable | 8 distinct signal bars (-35.1 to -49.6 bps). An engine timing bug ("decision snapshot too old") cancelled all 8 entries. The fix was already in the code but not yet installed on the trading machine. |

The fast-simulator rows used live-like costs of 5 + 0.5 bps per side, plus 1.5 bps on stops. The stress figure there assumes 14 bps per round trip. According to that review, the simulator gave the same trade counts as the production stand on the development data.

**Gate K:** never submitted. The official exact-engine run over the whole development history lost money (-198.30 USDT), so it would fail the gate K check "net above 0".
**Gate D:** not submitted.

From November 2026 the persona's Live slot traded H10-ETH instead (see `sonnet-h10-eth`).

## Why it is not enough

- **Edge equals cost.** The measured gross edge (about +13 bps) is about the round-trip cost (about 14 bps). With 7 bps of extra costs, or with an entry 30 s later, the result was about -7 bps per trade.
- **Negative out of sample.** -10.47 bps per trade over 1,645 trades in 2025-07 to 2026-07-17 (z -6.08). -198.30 USDT over 16,966 trades on the whole history.
- **Selection bias.** It was picked from about 920 configurations on 61 days of data. The author called this the winner's curse; a placebo against random entries does not correct for that selection.
- **Regime dependence.** Positive in only 4 of 9 weekly blocks. About -17 bps on down days against about +11 bps on up days (ex post). In a long decline it buys again an hour or more later and collects stops.
- **Fragile threshold.** On the combined data, thresholds of -30, -40 and -50 bps gave about -8, -5 and -9 bps per trade. Only -60 gave about +10 bps, on 35 trades.
- **Safe in Live only by accident.** The AI veto let through only about 1 to 13 % of the signals (confidence 0.645 +/- 0.024 against a 0.70 threshold). Without it, the recipe would have traded about 4 times a day at about -10 bps per trade.

## If you want to continue

- **Use it as a baseline, not as a strategy.** It shows the cost wall of minute-scale entries with taker orders.
- **Add a regime filter from higher timeframes** (trend, volatility or funding), and buy drops only where they have bounced before. Pre-register the rule and judge it only on data after the freeze. A related idea from the same line was a deeper "capitulation bounce": a 5-minute drop of at least 100 bps with RSI below 25, stop 100 bps, target 300 bps, hold up to 4 hours. It earned +19.5 bps per trade on 2024-01 to 2025-06 (177 trades) and then +1.89 bps per trade on 73 trades in the next period (2025-07 to 2026-07).
- **Try a passive entry below the market.** In the team's cost model a maker entry costs 2 bps fee + 2 bps adverse selection, against 5 + 2 bps for a taker. Measure the adverse selection on real fills.
- **Do not use an AI veto as a quality filter without testing it.** The team found that the veto's confidence grew with the number of rule conditions, not with market quality. Its market labels did not predict price (correlation about 0 over 800 calls).

## Reproduce

The recipe runs on the official evaluator's exact engine (rules only, no AI veto). Market data is not in this repository; you need a BTC-USDT-SWAP store of 1-minute candles and funding from OKX's public API, built as described in the repository QUICKSTART.

```
py -3.12 -X utf8 -B tools/strategy_workshop.py validate strategies/sonnet-v1-dump-buy/recipe.json
```

Expected `recipe_sha256`: `1ef1aaf257b130d14c1c97c9ecb43b72077d278c91c4449ad3b09dd3146ba082`. The sha256 of the file `recipe.json` itself: `4e27a2954f16189dd1eefc3236055e53a3446dcf5eb63c0c44901346df740a35`.

```
py -3.12 -X utf8 -B tools/team_eval.py run --recipe strategies/sonnet-v1-dump-buy/recipe.json --author you --label V1-sonnet-dev --set dev --store <store>/BTC-USDT-SWAP --out runs/v1_sonnet_dev
```

This engine is slow. The recorded whole-history run took 1,339 to 1,526 s with 10 to 14 worker processes (`--workers`, default 8), and the evaluator stops if free RAM drops below 6 GB. That run (code release of 2026-10-29) gave 16,966 trades and -198.302088 USDT. It is the only whole-history V1 run recorded, so later code versions may differ slightly. The fast-simulator rows and the P1 benchmark came from earlier research code and are not reproduced by this command.
