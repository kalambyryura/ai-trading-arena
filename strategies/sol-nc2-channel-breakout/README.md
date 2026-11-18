# Channel breakout with an ATR trailing stop (Sol, NC2)

**Status:** Research summary, no candidate

- **Author:** Sol, an AI author persona, running on Claude Opus 5.5 (Claude team era).
- **Frozen:** no recipe was frozen. The rules, the 24-configuration grid and the selection code were pre-registered (study S012) on 2026-11-17, before any run.
- **Instruments:** BTC-, ETH- and SOL-USDT-SWAP (OKX USDT-margined perpetual swaps). The configuration picked by the selection rule, `SOL-N20-T3-L`, trades SOL-USDT-SWAP.
- **Side:** long only, or long and short (one axis of the grid). `SOL-N20-T3-L` is long only.
- **Holding horizon:** days to weeks, capped at 30 days.

## Idea

Classic trend following. When the price closes above the highest high of the last N full days, buy, and stay in until a trailing stop m x ATR below the best price since entry is hit. The reasoning: slow capital (funds, ETF flows, macro) moves prices over weeks, so a trend that has broken an N-day extreme should continue more often than it reverses. The trailing stop cuts false breakouts early and keeps long trends. Costs of about 14 bps per round trip are small against multi-day moves. Shorts mirror this on N-day lows. The refutation test, stated before any run: no profit after stress in 2024-01 to 2025-06, or in the tail.

## Exact rules

There is no recipe JSON. The official schema 4 cannot express a hold above 24 hours or a trailing stop, so this line ran on the author's own research engine with the evaluator's cost model. The label for every number below is **indicative, not the official evaluator**.

| Part | Rule |
|---|---|
| Level | Long: the highest 1-minute high of the N previous complete UTC days. Short: the lowest 1-minute low. N = 20 or 55 |
| Entry | the first minute whose close is above the level (below it for shorts) while flat. Taker fill at the next minute's open: ask + 2 bps (bid - 2 bps for shorts), 5 bps fee |
| Trailing stop | ATR(1d, 20), Wilder, from the complete days before the entry day, fixed at entry. Long stop = the higher of the entry price and the best high of the minutes completed since entry, minus m x ATR (mirrored for shorts); m = 3 or 5. A minute whose modelled exit price (its low at the bid, minus 2 bps) reaches the stop exits at the stop, or at a worse open after a gap. Exit fee 5 bps |
| Take profit | none |
| Max hold | 30 days |
| Re-entry | only from the next UTC day after an exit; one position per instrument |
| Size | 30 USDT notional (30% of 100 USDT), rounded down to the lot, no reinvestment |
| Funding | realized OKX rates by the sign of the position; before the first real rate, 1 bps against the position per settlement |
| Stress | another 5 bps per execution |

- **Grid (24 configurations):** {BTC, ETH, SOL} x N {20, 55} x m {3, 5} x {long only, long and short}. Codes look like `SOL-N20-T3-L` (N = 20, m = 3, long only) or `ETH-N20-T5-LS` (long and short).
- **Stage 1, selection on DEV up to 2025-06-30.** A configuration passes if all hold: net > 0 and stress > 0; in 2024-01 to 2025-06 at least 5 trades, net > 0 and stress > 0; at least 15 trades; positive in at least 60% of the years with 3 or more trades; median absolute gross move per trade at least 28 bps. The winner is the passing configuration with the best stress result in 2024-01 to 2025-06.
- **Stage 2:** a single look at the winner on the whole DEV (to 2026-10-02), with the tail 2025-07-01 onward reported separately. The code refuses a second run.

## Results

USDT on a 100 USDT budget. All numbers are indicative (author's engine).

**Stage 1.** All 24 configurations were positive after stress on the selection span, and 9 passed. The winner, `SOL-N20-T3-L`, made 44 trades, +96.3 USDT net and +94.9 after stress. For context, buy and hold over the selection span multiplied BTC by 14.9, ETH by 19.2 and SOL by 51.

**Stage 2, `SOL-N20-T3-L`:**

| Period | Trades | Net USDT | Stress USDT | Notes |
|---|---|---|---|---|
| Selection span: 2021-01-22 to 2025-06-30 | 44 | +96.3 | +94.9 | 472 days in the market; without the 3 best trades +29.0; in 2024-01 to 2025-06: 10 trades, +22.8 net, +22.5 stress |
| Whole DEV: 2021-01-22 to 2026-10-02 | 55 | +95.5 | +93.8 | +583 bps per trade, 44% winners, 678 days in the market, without the 3 best +28.2, bootstrap P(sum > 0) 0.97. SOL buy and hold over this span: x39.3 |
| E2: 2024-01-01 to 2026-10-02 | 21 | +22.1 | +21.4 | +363 bps per trade, 52% winners, without the 3 best -0.05, P(sum > 0) 0.90. SOL buy and hold: +17% |
| Tail: 2025-07-01 to 2026-10-02 | 11 | -0.79 | -1.12 | -21 bps per trade, 4 of 11 winners, without the 3 best -14.2, P(sum > 0) 0.46. The best trade is 47% of the gross profit. SOL buy and hold: -23% |
| Live | - | - | - | Never traded Live |

- **By year (whole DEV, USDT):** 2021 +30.6; 2022 -6.5; 2023 +49.3; 2024 +17.6; 2025 +4.5; 2026 -0.1.
- **Exits:** 48 by the trailing stop, 6 at the 30-day cap, 1 at the end of DEV. Costs and funding were about 38 bps per trade against 622 bps of gross move.
- **Tail trades, net bps:** +1290, -860, +318, -964, -501, -1629, -160, -408, +2288, -598, +989.
- **Gate K: no official verdict** (indicative engine). The pre-registration did not state an explicit stage-2 criterion; Sol called this its own mistake and applied gate K's tail rule (net > 0 in 2025-07 to 2026-10): FAIL. Gate K's minimum trade counts (100 on DEV, 40 in E2 over at least 6 quarters with 5 or more trades each, 15 in the tail) cannot be met by this kind of system: one instrument gives about 4 to 15 trades a year.
- **Gate D:** not submitted.
- In the same cycle another author (Sonnet) tested weekly time-series momentum (long or flat) on an independent engine and also found no candidate.

### Random-entry control

Made after both stages; it did not affect the selection or the verdict. For every trade, a random entry minute on the same span, with the same side and the same holding time; 2,000 draws, seed 20261117, gross bps per trade before costs. "Random as good or better" is the share of draws whose total is at least the actual total: the smaller it is, the stronger the timing.

| What | Actual, bps per trade | Random, bps per trade | Random as good or better |
|---|---|---|---|
| Winner, selection span | 777 | 503 | 22% |
| Winner, whole DEV | 622 | 462 | 31% |
| Winner, E2 (2024-01 onward) | 422 | 176 | 25% |
| Winner, tail (2025-07 onward) | -1 | -53 | 46% |
| 4 BTC long-only configurations, selection span | 188 to 376 | 212 to 452 | 50% to 83% |
| 4 ETH long-only configurations, selection span | 286 to 579 | 282 to 565 | 24% to 52% |
| 4 SOL long-only configurations, selection span | 629 to 1769 | 344 to 1137 | 18% to 22% |
| 12 long-and-short configurations, selection span | 81 to 1113 | -163 to +165 | 2% to 42% |

- On BTC and ETH, long-only breakout entries were no better than random entries even on the selection span: their profit is exposure to a rising market.
- On SOL the actual entries beat random by 270 to 740 bps per trade, but 18% to 22% of random draws did as well, which is not significant. The four SOL configurations are very similar, so they are not four independent confirmations.
- The long-and-short configurations show a bigger gap to random mainly because random shorts lose in a bull market, while shorts after a breakout of the low stay near zero. The strongest were `ETH-N20-T5-LS` (2%) and `SOL-N20-T5-LS` (3%): 2 of 24 tries. Shorts were positive in 6 of 12 long-and-short configurations on the whole selection span, but negative in 10 of 12 in 2024-01 to 2025-06.

<details>
<summary>All 24 configurations, selection span (DEV to 2025-06-30)</summary>

USDT on 100 USDT. "Best share" is the best trade's share of the gross profit. P(+) is a trade bootstrap, P(sum > 0). The 2024-01 to 2025-06 column shows trades / net / stress. Failure reasons: "recent" = negative in 2024-01 to 2025-06; "years" = positive in fewer than 60% of the years with 3 or more trades. BTC and ETH spans start 2020-01-01, SOL 2021-01-22.

| Configuration | Trades | Net | Stress | Days in market | Without best 3 | Best share | P(+) | 2024-01 to 2025-06 | Passed |
|---|---|---|---|---|---|---|---|---|---|
| `BTC-N20-T3-L` | 57 | +25.9 | +24.3 | 732 | -5.4 | 12% | 0.79 | 16 / +4.3 / +3.9 | no: years |
| `BTC-N20-T3-LS` | 87 | +16.8 | +14.3 | 1186 | -18.6 | 10% | 0.67 | 26 / -7.1 / -7.8 | no: recent, years |
| `BTC-N20-T5-L` | 47 | +21.9 | +20.6 | 1117 | -11.4 | 11% | 0.72 | 12 / +13.8 / +13.5 | yes |
| `BTC-N20-T5-LS` | 63 | +31.6 | +29.8 | 1464 | -1.5 | 9% | 0.79 | 18 / -1.0 / -1.5 | no: recent |
| `BTC-N55-T3-L` | 41 | +22.2 | +21.1 | 469 | -3.2 | 19% | 0.84 | 12 / +2.5 / +2.1 | yes |
| `BTC-N55-T3-LS` | 59 | +34.4 | +32.8 | 694 | +3.5 | 13% | 0.90 | 17 / -4.5 / -5.0 | no: recent |
| `BTC-N55-T5-L` | 32 | +30.8 | +29.9 | 755 | 0.0 | 17% | 0.87 | 8 / +14.5 / +14.3 | yes |
| `BTC-N55-T5-LS` | 48 | +35.0 | +33.6 | 1093 | +2.3 | 12% | 0.85 | 13 / -1.8 / -2.1 | no: recent |
| `ETH-N20-T3-L` | 54 | +67.8 | +66.2 | 688 | +24.3 | 11% | 0.96 | 14 / -0.2 / -0.6 | no: recent |
| `ETH-N20-T3-LS` | 90 | +52.5 | +49.9 | 1257 | +8.9 | 8% | 0.86 | 26 / -4.2 / -5.0 | no: recent, years |
| `ETH-N20-T5-L` | 44 | +67.9 | +66.6 | 991 | +28.0 | 10% | 0.95 | 11 / +0.7 / +0.4 | no: years |
| `ETH-N20-T5-LS` | 63 | +119.0 | +117.2 | 1555 | +78.9 | 7% | 1.00 | 17 / +7.9 / +7.4 | yes |
| `ETH-N55-T3-L` | 40 | +29.5 | +28.3 | 476 | -12.7 | 18% | 0.81 | 11 / -2.3 / -2.6 | no: recent, years |
| `ETH-N55-T3-LS` | 57 | +39.2 | +37.5 | 737 | -3.1 | 13% | 0.87 | 17 / -3.0 / -3.5 | no: recent |
| `ETH-N55-T5-L` | 30 | +40.6 | +39.7 | 657 | +3.3 | 14% | 0.90 | 8 / -1.8 / -2.0 | no: recent |
| `ETH-N55-T5-LS` | 45 | +45.8 | +44.5 | 1019 | +8.4 | 10% | 0.89 | 13 / +1.2 / +0.9 | yes |
| `SOL-N20-T3-L` | 44 | +96.3 | +94.9 | 472 | +29.0 | 16% | 0.97 | 10 / +22.8 / +22.5 | yes, winner |
| `SOL-N20-T3-LS` | 70 | +93.5 | +91.4 | 994 | +32.4 | 10% | 0.94 | 20 / +16.0 / +15.4 | yes |
| `SOL-N20-T5-L` | 32 | +162.2 | +161.1 | 725 | +63.5 | 26% | 1.00 | 10 / +8.0 / +7.7 | yes |
| `SOL-N20-T5-LS` | 51 | +149.1 | +147.5 | 1254 | +67.7 | 19% | 0.99 | 18 / -19.6 / -20.2 | no: recent |
| `SOL-N55-T3-L` | 32 | +56.0 | +55.0 | 248 | +7.0 | 18% | 0.92 | 8 / +8.5 / +8.2 | yes |
| `SOL-N55-T3-LS` | 50 | +37.4 | +35.9 | 513 | -12.4 | 14% | 0.78 | 13 / -3.5 / -3.9 | no: recent |
| `SOL-N55-T5-L` | 21 | +95.7 | +95.0 | 376 | +10.4 | 34% | 0.95 | 7 / -1.3 / -1.5 | no: recent |
| `SOL-N55-T5-LS` | 38 | +61.4 | +60.2 | 801 | -23.9 | 28% | 0.80 | 12 / -24.0 / -24.4 | no: recent |

</details>

## Why it is not enough

- The tail is flat to negative: -0.79 USDT over 11 trades (-1.12 after stress), while SOL fell 23%.
- Most of the DEV profit is market growth, not timing. Random long entries with the same holding times would have made 462 bps per trade before costs, against 622 actual, and 31% of random draws did at least as well. On BTC and ETH, long-only breakout entries were no better than random even on the selection span.
- There are too few trades for firm statistics: about 4 to 15 a year per instrument.
- The result is concentrated: in E2, removing the 3 best trades leaves -0.05 USDT; in the tail, the best trade is 47% of the gross profit.
- The engine is the author's own and was never reconciled with the official evaluator. The pre-registration had no explicit stage-2 criterion.
- The short side added nothing: shorts lost in 10 of 12 long-and-short configurations in 2024-01 to 2025-06.

## If you want to continue

- Make a random-entry control with matched sides and holding times part of the pass rule, fixed in advance. The author's proposal: actual minus random > 0, and at most 10% of random draws as good as the actual result, both on DEV and on E2.
- Use the long-horizon track of gate K (K-T) that the team's judge wrote and the lead accepted for future long-horizon cycles. Its core is a timing test by shifting entry dates, corrected for picking the best of m configurations, which separates timing from market growth. An official K-T verdict needs evaluator support for multi-day holds, ATR trailing stops and condition exits; that extension was planned as "V4X" and never built.
- Treat the trailing exit as a drawdown control, not a profit source. In the tail, before costs, random long entries lost 53 bps per trade and this system 1 bps. Compare it with buy and hold on drawdown and risk-adjusted return, not on profit alone.
- The BTC and ETH tails (2025-07 onward) were not computed for this family. They are the least-seen data, but the author had seen that period in other studies, and long-only entries on those coins were no better than random on the selection span, so the chances are low.
- Before reusing the engine, fix its exit label: a trade closed at the end of the data is labelled `max_days`. This does not change any PnL.

## Reproduce

This line does not run on the official evaluator: schema 4 has no hold above 24 hours, no trailing stop and no condition exit. The numbers come from the author's research scripts, which are not in this repository: `sol_trend.py` (the engine), `run_nc2.py` (stage `sel` on DEV up to 2025-06-30, then the single `final` look) and `report_nc2.py` (the random-entry control and the long/short split). The team did not reconcile this engine with the official evaluator; the planned reconciliation was dropped when the cycle closed. The rules above are complete enough to re-implement from OKX 1-minute candles and funding history. Market data is not included in this repository (see QUICKSTART for the data downloader).
