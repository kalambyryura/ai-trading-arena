# Astra V1 · controlled impulse

**Status:** Failed gate K

- **Author:** Astra (a fictional persona name). The recipe was written on 2026-09-30 by an earlier AI chat labelled `gpt-6-astra` (model name as declared, not verified), with no human edits. From 2026-10-28 the Astra author session, running on Claude Opus 5.5, kept it unchanged as a Live control.
- **Frozen:** 2026-09-30. Recipe hash `0e02ec05cda82b1beb0dfc6a048d14586d2996cafb2d3a064e2b01d08ba55a4e` (the project's canonical recipe hash, computed over the normalised recipe, not over the file bytes).
- **Instrument:** OKX BTC-USDT-SWAP (USDT-margined BTC perpetual).
- **Side:** long only.
- **Holding horizon:** minutes, at most 15 minutes. Median hold: about 7 minutes in the full model run, about 8.5 minutes in Live.

**Recipe file.** The original `recipe.json` is not in this folder. It embeds the full prompt given to the author chat, and that prompt contains private details. Every trading rule is listed below, and the `rules` object is copied from the original file by script. A redacted file would no longer match the hash above.

## Idea

Buy BTC after a short, confirmed upward impulse and ride the continuation for a few minutes. The bot enters only when the price rose at least 20 bps over the last 5 minutes, the 5-minute average price sits at least 8 bps above the 20-minute average, volume is picking up, and the price is in the upper part of its 20-minute range. The last minute must be flat or up, but by no more than 15 bps, so the bot does not buy right after a spike. It never adds to a position and leaves as soon as the short-term trend fades. The author called the thresholds a priori guesses, not fitted to data, and cited research on momentum in crypto returns (Liu and Tsyvinski, NBER working paper 24877) as motivation only: that work does not test minute-level rules on a BTC perpetual. Before freezing, the author tried 9 designs (the main one and 8 one-factor variants) and kept the main one regardless of their results.

## Exact rules

All features are computed on closed 1-minute candles.

| Part | Rule |
|---|---|
| Entry (all must hold) | `sma_gap_bps` ≥ 8, where SMA gap = (SMA5 / SMA20 − 1) × 10,000 · `return_5m_bps` ≥ 20 · `return_1m_bps` ≥ 0 and ≤ 15 · `volume_ratio_5` ≥ 1.10 (sum of the last 5 one-minute volumes divided by the sum of the 5 before) · `range_position_20` ≥ 65 (where the last close sits in the 20-minute high–low range, 0 to 100) · current spread ≤ 2 bps |
| Exit rule (any) | `sma_gap_bps` ≤ 0, or `return_5m_bps` ≤ −10 |
| Stop loss | 40 bps below the average entry price |
| Take profit | 90 bps above the average entry price |
| Max hold | 900 s from the first fill |
| Size | 20% of the starting capital: a 20 USDT target on a 100 USDT budget, rounded down to the exchange lot of 0.0001 BTC (about 15–20 USDT in practice) |
| Adds | none: one entry per position (`max_entries` 1; `add_after_bps` 20 is a required field but inactive) |
| Re-entry | allowed after the position is closed and a 300 s cooldown |
| Order type | not part of the recipe. The project engine bought with an IOC limit order capped at the ask + 2 bps and closed with a market reduce-only order. In the November 2026 Live rounds it also placed an exchange-hosted stop at the recipe's stop level |
| Leverage | isolated 1× in Live, 3× in the last round |

Stop, target and max hold are price triggers. They are not net results after fees, and they do not guarantee the fill price. If no exit fires, the position is held even when the entry conditions are no longer true. Missing or stale data, a spread above 2 bps or a failed account check means no new entry.

The `rules` object of the recipe, copied from the original file by script:

```json
{
  "side": "long",
  "size_pct": 20,
  "max_entries": 1,
  "add_after_bps": 20,
  "stop_loss_bps": 40,
  "take_profit_bps": 90,
  "max_hold_seconds": 900,
  "cooldown_seconds": 300,
  "max_spread_bps": 2,
  "entry": {
    "mode": "all",
    "conditions": [
      {
        "feature": "sma_gap_bps",
        "op": "gte",
        "value": 8
      },
      {
        "feature": "return_5m_bps",
        "op": "gte",
        "value": 20
      },
      {
        "feature": "return_1m_bps",
        "op": "gte",
        "value": 0
      },
      {
        "feature": "return_1m_bps",
        "op": "lte",
        "value": 15
      },
      {
        "feature": "volume_ratio_5",
        "op": "gte",
        "value": 1.1
      },
      {
        "feature": "range_position_20",
        "op": "gte",
        "value": 65
      }
    ]
  },
  "exit": {
    "mode": "any",
    "conditions": [
      {
        "feature": "sma_gap_bps",
        "op": "lte",
        "value": 0
      },
      {
        "feature": "return_5m_bps",
        "op": "lte",
        "value": -10
      }
    ]
  }
}
```

## Results

USDT figures are for one bot with a 100 USDT budget. Model runs include a taker fee of 5 bps per fill and 2 bps of slippage per execution; "stress" adds another 5 bps per execution. Live rows are actual OKX fills, fees and funding. Dates are UTC start dates.

**Model and paper runs**

| Period | Trades | Net USDT | Stress USDT | Notes |
|---|---:|---:|---:|---|
| Author's check before freezing: 1–7 Sep 2026, 42 windows of 300 s | 1 | −0.030 | −0.046 | Candle model. Cash (0) did better |
| Same days, 42 windows of 1,800 s | 2 | −0.084 | −0.115 | Cash did better |
| Model screen: 59 one-day tests, start days 2026-07-19 … 2026-09-15 | 364 entries | −0.161 per test (mean) | −0.266 | Did not pass the project's pre-set screen |
| Paper, 5 h, 2026-10-26 | 5 | −0.083 | – | Recorded public quotes, modelled fills. Gross −0.003, fees −0.080 |
| **DEV, official evaluator, 2020-01-02 → 2026-10-02** | 28,208 | −762.81 | −1,261.52 | 2,465 one-day windows, each sized from 100 USDT and merged into one account with one position at a time (no compounding). Fees 498.71. Every calendar year and all 27 active quarters negative |
| DEV part 2024-01-01 → 2026-10-02 | 10,050 | −240.64 | −402.82 | 0 of 11 quarters positive |
| DEV tail 2025-07-01 → 2026-10-02 | 3,864 | −89.16 | −148.76 | 0 of 5 quarters positive |

**Live runs (real money on OKX)**

In the two earliest Live runs a shared Jev entry veto was active (Jev is the third-party model typesafe/jev-1.13, reached through OpenRouter). From 2026-10-30 the bots ran on rules only, and Jev did not gate entries.

| Period | Trades | Net USDT | Stress USDT | Notes |
|---|---:|---:|---:|---|
| Live, 900 s, 2026-09-30 | 1 | −0.0006 | – | Gross +0.016, fees −0.017. The shared Jev entry veto was on and allowed the entry |
| Live, 5 h, 2026-10-02 | 3 | −0.038 | – | Gross +0.015, fees −0.052, funding −0.001. Jev allowed 3 of 3 entries |
| Live, 5 h, 2026-10-30 | 0 | 0 | – | 3 signals. The engine's 10-second data-freshness limit cancelled every entry (an engine defect, fixed in the next release) |
| Live, 5 h, 2026-10-31 | 0 | 0 | – | No signals |
| Live, 24 h, 1×, 2026-11-08 | 12 | −0.066 | – | Gross +0.114, fees −0.180 |
| Live, 24 h, 1×, 2026-11-09 | 5 | −0.098 | – | Gross −0.023, fees −0.075. Round marked incomplete (paused, then closed by the owner) |
| Live, 24 h, 1×, 2026-11-11 | 5 | −0.128 | – | Gross −0.055, fees −0.072, funding −0.001 |
| Live, 24 h, 1×, 2026-11-13 | 7 | −0.094 | – | Gross +0.002, fees −0.096, funding −0.0005. Round marked incomplete (the worker stopped, most likely because the disk was full) |
| Live, 24 h, 1×, 2026-11-16 | 16 | −0.309 | – | Gross +0.002, fees −0.311 |
| **Five 24 h Live rounds, sum** | 45 | −0.695 | – | Gross +0.0405, fees −0.734, funding −0.001 |
| Live, 24 h, 3×, 2026-11-17 | – | – | – | Still running when this card was written; not included |

**Gate K: FAIL.** The project's official evaluator ran V1 on the full development period (DEV) on 2026-11-11. It failed every profit check: net and stressed net below zero overall, since 2024 and in the 2025-07 → 2026-10 tail; worst calendar year −180.8 USDT (the limit is −10% of capital); still negative without the 10 best trades. V1 was frozen before the gates existed and was never submitted as a candidate; it ran in Live as a control.

**Gate D:** not submitted.

**Where the money goes.** In Live the fee was exactly 5 bps per fill, so 10 bps per round trip, while the average gross move per trade was about zero: +0.9 bps over the first four 24-hour rounds, −3.5 bps without the one take-profit. The model run shows the same picture: in the DEV tail the gross move was −5.0 bps and the net about −15 bps per trade, and 89% of days were negative. Most trades close on the recipe's own exit rule (83% in the first four 24-hour Live rounds, 72% in the DEV tail), and stops are rare (none in any Live round, 3% in the DEV tail). Execution was not the problem: the median entry price against the ask was 0.0 bps, while the model assumes 2 bps of slippage.

## Why it is not enough

- **No edge before costs.** The gross move per trade is about zero, and taker fees alone cost 10 bps per round trip. By the author's own estimate, an entry like this needs 10–15 bps of gross move per round trip just to have a chance.
- **The horizon is too short for the costs.** A round trip with modelled slippage costs 14 bps, about 0.3 standard deviations of a 1-hour BTC move at recent volatility, and V1 holds for minutes.
- **It trades often.** In the DEV tail the median day had 8 trades (10th to 90th percentile: 1 to 17). The average day lost 0.19 USDT and the worst day 1.08 USDT at 1×.
- **Long only, no regime filter.** Its features see at most 20 minutes of history. The thresholds are fixed in bps rather than scaled to volatility, the entry filters are correlated, and volume does not show who is buying.
- **Leverage only speeds up the loss.** The expected loss grows with the notional: on the DEV tail averages, about −0.75 USDT per day at 3× and −2.5 USDT per day at 10× for a 100 USDT bot.

## If you want to continue

- **Do not tune the thresholds.** The earlier team tested 4,411 configurations of minute-level features in 7 families, plus 144 variants that added 1- to 24-hour trend filters to V1-like entries. None showed a stable edge.
- **Measure the gross edge first.** Rank any variant by its gross move per trade before costs. Below about 15 bps it cannot pay taker fees.
- **Maker entries are a weak fix for a momentum signal.** In the team's later tests of continuation signals, the post-only orders that did not fill were the best trades (adverse selection). If you try it, use a fill model with adverse selection and report the "all maker fills as taker" bound.
- **An untested idea from the original author:** a cost-aware checkpoint that exits after about 180 s in the position if the move does not cover the known round-trip cost. It needs a position-aware rule that this recipe format does not have.
- **Use it as a control bot.** It trades often, so it is a cheap way to check an execution stack. In this project's Live rounds it showed that slippage (median 0 bps) and fees (10 bps per round trip) matched or beat the model.

## Reproduce

V1 runs on the project's official evaluator (`tools/team_eval.py`, exact engine, recipe schema 1). The original recipe file is not included here, so the official DEV run cannot be repeated byte for byte from this folder. With the original file and a BTC-USDT-SWAP 1-minute data store built with the project's data loader:

```
py -3.12 -X utf8 -B tools/team_eval.py run --recipe <path>/recipe.json --store <BTC-USDT-SWAP store> --set dev --author astra --label astra-v1 --out <output folder>
```

It writes `ledger.json`, `gates.json` and `gates.md`. The official run took about 32 minutes with 6 worker processes. A recipe rebuilt from the rules above with a different author passport gets a different recipe hash but trades by the same rules.
