# SOL-LU-M25-H12: maker pullback near the 7-day high (Sol, NC1)

**Status:** Failed gate K

- **Author:** Sol, an AI author persona, running on Claude Opus 5.5 (Claude team era).
- **Frozen:** 2026-11-17. The recipe file was built and committed together with its pre-registration (study S011) before any run.
- **Instrument:** SOL-USDT-SWAP (OKX USDT-margined perpetual swap).
- **Side:** long only.
- **Holding horizon:** up to 12 hours.
- **Recipe file:** `SOL-LU-M25-H12.json` (schema 4), an exact copy of the evaluated file. File sha256 `f31118444c0808435a39b7b0a4a7c147d6b0a815e77d8c81a4ebd7fc6003b80e`; canonical `recipe_sha256` `f81347f15a3cafe6e748507e25d2f8279d860d379881f8991eb05ca86ef6216a`. Its `description` and `original_text` fields hold the author's original Ukrainian text.

## Idea

The hypothesis: when SOL trades above its 20-day average and a one-hour push brings the price just below its 7-day high, the move continues for the next 12 to 24 hours. Sol's earlier studies on the same history pointed that way for the long side; the short side was flat or negative. Instead of chasing the push with a market order, this recipe rests a post-only limit buy a little below the signal price. That should cost less (4 bps in the model instead of 7) and buy an ordinary pullback instead of the top of the push. The known price of this choice is missing the trades where the price runs on without pulling back. The study pre-registered 16 configurations (BTC and SOL; long in uptrends and short in downtrends; limit offset 0.25 or 0.5; hold 12 or 24 hours) plus 8 controls. This is the configuration the pre-registered selection rule picked.

## Exact rules

| Part | Rule (recipe field) |
|---|---|
| Regime | `sma_gap_bps(1d, 20) > 0`: the latest 1-minute close is above the average of the last 20 closed daily closes |
| Near the 7-day high | `-30 <= dist_high_bps(1d, 7) <= -5`: the price is 5 to 30 bps below the highest high of the last 7 closed daily bars |
| Push | `return_bps(1m, 60) >= 30`: the price rose at least 30 bps over the last 60 minutes |
| Order | `post_only` limit buy placed `0.25 x rms_bar_bps(1h, 168)` below the signal bar's close. `rms_bar_bps(1h, 168)` is the root mean square of the open-to-close moves of the last 168 hourly bars, in bps. If it is not filled within 3,600 s it is cancelled and there is no trade |
| Stop loss / take profit | 1,500 bps / 3,000 bps (wide, protection only) |
| Max hold | 43,200 s (12 h) |
| Cooldown | 3,600 s after an exit |
| Size | 30% of 100 USDT; one position at a time |

How the official schema-4 evaluator models it (fill model `v4-path5s-2`):

- One rule decision per newly closed bar.
- A post-only limit is rejected if it would cross the book when placed. It fills only if the modelled price path passes the limit by at least max(1 bps, 2 ticks) before the timeout. A fill pays a 2 bps fee plus 2 bps for adverse selection.
- Exits pay a 5 bps taker fee. The stop is live from the fill and the target from the next minute; when both are reachable in the same minute, the stop wins.
- Realized funding is charged; where funding history is missing, a conservative charge against the position is used.
- The stress case subtracts another 5 bps of every executed notional.

## Results

USDT on a 100 USDT budget, from the official evaluator under the gate's conservative funding assumption. Net uses the evaluator's costs; stress adds 5 bps per execution.

| Period | Trades | Net USDT | Stress USDT | Notes |
|---|---|---|---|---|
| Selection span: DEV 2021-01-22 to 2025-06-30 | 265 | +17.0 | +9.2 | Passed all 8 pre-registered selection conditions. Lower bound with every maker fill charged as taker: +14.0. In 2024-01 to 2025-06: 103 trades, +48.3 bps per trade, 56.2 bps above the regime-only control. 74% of post-only orders filled. |
| Whole DEV 2021-01-22 to 2026-10-02 (single look) | 335 | +20.0 | +10.1 | Without the 10 best trades: -10.3. Maker fill-model sensitivity: +16.1 to +20.2; lower bound +15.1. 24% of orders unfilled (106 of 441). |
| E2: 2024-01-01 to 2026-10-02 | not reported | +17.6 | +12.6 | Positive in 6 of 11 active quarters (55%). |
| E2 tail: 2025-07-01 to 2026-10-02 | 70 | +3.2 | +1.2 | +14.2 bps per trade. Both controls lost here: the taker version -32.8 bps and the regime-only control -14.2 bps per trade. |
| Live | - | - | - | Never traded Live. |

- **Gate K: FAIL** (official evaluator). Two checks failed: the result without the 10 best trades must be at least 0 (it is -10.3 USDT), and at least 60% of the active quarters in E2 must be positive (6 of 11, 55%). No other check failed.
- **Gate D:** not submitted.
- **Clone check** against H10 (ETH), the team's first gate K candidate: 6.9% entry overlap, 0.15 daily PnL correlation, so not a clone.

### Controls on the selection span

| Control | Trades | Net USDT | Stress USDT | 2024-01 to 2025-06 |
|---|---|---|---|---|
| Same entry with a taker order, 24 h hold | 242 | +40.3 | +33.1 | 87 trades, +24.2 bps per trade |
| Regime only: one entry a day at 00:00-00:04 UTC, post-only 0.25 x rms, 23.5 h hold | 561 | +58.5 | +41.9 | -7.9 bps per trade |

### Which post-only orders filled

Selection span. The trades of the taker control are split by whether the post-only order of the same signal minute filled. Numbers are bps per trade, trade counts in brackets.

| Configuration | Unfilled | Taker trade, maker filled | Taker trade, maker missed | Maker price gain on filled orders |
|---|---|---|---|---|
| SOL long, offset 0.25 (this recipe) | 28% | -14.8 (170) | +226.6 (72) | +60 |
| SOL long, offset 0.5 | 42% | -78.0 (130) | +213.8 (112) | +91 |
| BTC long, offset 0.25 | 21% | -7.6 (240) | +83.6 (81) | +19 |
| BTC long, offset 0.5 | 34% | -27.5 (195) | +81.8 (126) | +35 |

On continuation signals the limit order fills mostly when the push fails. Pushes that keep going leave it unfilled. The better price on the filled orders does not make up for that.

### Post-hoc split by market regime

Diagnostic only, made after the result with the team's four-regime classifier; it played no part in the selection. All trades of this recipe fall in up regimes by construction.

| Regime | Trades | Net USDT | bps per trade | Tail |
|---|---|---|---|---|
| Up, calm | 192 | +19.5 | +34.1 | 43 trades, +39.3 bps |
| Up, storm | 142 | +1.3 | +2.8 | 27 trades, -25.8 bps |

### Rest of the grid

- Of the 16 main configurations, only 2 passed selection: this one and `SOL-LU-M50-H12` (the same with a 0.5 offset).
- All four BTC long configurations were negative on the selection span (-9.9 to -0.1 USDT).
- No short configuration passed. Shorts near the 7-day low in a downtrend lost money after stress on BTC and SOL, with maker and with taker orders.

## Why it is not enough

- The profit rests on a few trades: without the 10 best, the whole-DEV result is -10.3 USDT.
- It is not stable in the recent epoch: positive in only 6 of 11 active quarters of 2024-2026.
- The tail edge is thin: +3.2 USDT over 70 trades (+1.2 after stress) on a 100 USDT budget, across five quarters.
- Maker entries pick the worse trades on this kind of signal (see the fill table). The maker fill model has never been checked against Live fills. The team's rules require at least two Live rounds with measured fills before a maker strategy can be called proven.
- The selection reused data the author had already studied: earlier studies on DEV up to 2025-03 and a diagnostic run of the same trigger. It picked 1 of 16 configurations. The tail was the only fresh check, and it has now been used.
- On the selection span both simpler controls earned more than this recipe.
- The post-hoc finding that the edge sits in calm uptrends has not been tested on new data.

## If you want to continue

- Forward test only: write a new pre-registration and use data after 2026-10-02, because this DEV has been looked at. The author's suggested next test: SOL long on the approach, maker entry, 12 h, only in the calm uptrend regime.
- Prefer a taker entry for continuation signals. The owner's strategy slot (MYHuman) reuses this entry signal with a taker order as one of the two branches of its recipe `SOL-NC1-K6-F0-EK`; see `myhuman-nc1-synthesis`.
- Measure post-only fill rates and the price move after each fill in Live before trusting any maker result.
- Use maker orders where the fill itself is part of the signal (an entry on a return to a level) or for exits, not for chasing a push.

## Reproduce

The recipe runs on the official evaluator (schema-4 candle engine):

```
py -3.12 -X utf8 -B tools/team_eval.py run --recipe strategies/sol-nc1-maker-pullback/SOL-LU-M25-H12.json --author sol --label SOL-LU-M25-H12 --set dev --store <data folder>/SOL-USDT-SWAP --out runs/SOL-LU-M25-H12
```

- Add `--end 2025-06-30` to run only the selection span.
- The store must hold OKX SOL-USDT-SWAP 1-minute candles and funding history. Market data is not included in this repository; build the store with the repository's data downloader (see QUICKSTART).
- The run writes `ledger.json`, `gates.json` and `gates.md` to the `--out` folder. With the team's data and code it gave 335 closed trades, net +19.99 USDT, stress +10.10 USDT, and gate K FAIL on the two checks above. A fresh download of the data can differ slightly.
