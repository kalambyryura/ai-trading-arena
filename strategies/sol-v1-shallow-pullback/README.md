# Shallow Pullback (Sol V1)

**Status:** Not proven

- **Author:** Sol, an AI author persona. This first version was written on 2026-09-30 by an earlier chat labelled `gpt-6.1-sol` (the model name is as the chat declared it, not verified). The original recipe is named "SOL · Shallow Pullback"; "SOL" there is the persona, not the coin.
- **Frozen:** 2026-09-30. Canonical recipe fingerprint `87facc50e7f925638106c9572bcaf1abb17295fd428418b59cede3b4c09f0191`.
- **Instrument:** BTC-USDT-SWAP (OKX USDT-margined perpetual swap).
- **Side:** long only.
- **Holding horizon:** at most 15 minutes per trade. It was designed for 5- to 30-minute matches. Later it ran unchanged in 24-hour Live rounds, entering again after each exit.

## Idea

Buy a shallow one-minute dip inside a short-term uptrend on BTC. When the 5-minute average is above the 20-minute average and the price has risen over the last five minutes, a small red minute is treated as a pause, not a reversal. Buying that dip was meant to give a better price than buying the strong green minute itself. Two filters ask for the close to sit in the upper part of its 20-minute range and for volume not to be falling. The author called the hypothesis a guess, not a confirmed edge, and fixed all thresholds before downloading any price history.

## Exact rules

All features come from closed 1-minute candles, as defined by the arena's shared 7-feature contract:

- `sma_gap_bps` = (SMA5 / SMA20 - 1) x 10000
- `return_1m_bps` = (last close / previous close - 1) x 10000
- `return_5m_bps` = (last close / close 5 minutes earlier - 1) x 10000
- `range_position_20` = position of the close inside the high-low range of the last 20 minutes, 0 to 100
- `volume_ratio_5` = volume of the last 5 closed minutes / volume of the 5 minutes before

An unknown feature value blocks a new entry.

| Part | Rule |
|---|---|
| Entry (all must hold) | `sma_gap_bps >= 12`; `return_5m_bps >= 20`; `-20 <= return_1m_bps <= -2`; `range_position_20 >= 60`; `volume_ratio_5 >= 1.0` |
| Spread gate | no entry if the bid-ask spread is above 1.5 bps (`max_spread_bps`) |
| Exit (any one) | `sma_gap_bps <= 0` or `return_5m_bps <= -15` |
| Stop loss / take profit | 40 bps / 90 bps price move from the entry price |
| Max hold | 900 s from the first fill |
| Cooldown | 180 s after a full close; then a new entry under the same rules is allowed |
| Size | 30% of the starting 100 USDT budget, so at most 30 USDT notional, rounded down to the lot |
| Adds | none: `max_entries` is 1, so `add_after_bps` (20) never applies |
| Order type | not part of the recipe. The evaluator charges a 5 bps fee and 2 bps slippage per execution plus a 1-tick spread. In Live, entries were IOC limit buys at the ask x 1.0002 and exits were market reduce-only orders, with an exchange-side stop at the recipe's stop level |
| Jev | in the arena design, Jev (the third-party model `typesafe/jev-1.13`, called through OpenRouter) could veto an entry that the rules already allowed; it could never force one. In all Live rounds below it ran in shadow mode and decided nothing |

The recipe file is not published in this folder. Its author block embeds the original authoring prompt, which contains private workspace details. The fingerprint covers that block, so a cleaned copy would not match `87facc50…`. Below is the `rules` block of the original file, with the same content:

```json
{
  "side": "long",
  "size_pct": 30,
  "max_entries": 1,
  "add_after_bps": 20,
  "stop_loss_bps": 40,
  "take_profit_bps": 90,
  "max_hold_seconds": 900,
  "cooldown_seconds": 180,
  "max_spread_bps": 1.5,
  "entry": {
    "mode": "all",
    "conditions": [
      {
        "feature": "sma_gap_bps",
        "op": "gte",
        "value": 12
      },
      {
        "feature": "return_5m_bps",
        "op": "gte",
        "value": 20
      },
      {
        "feature": "return_1m_bps",
        "op": "gte",
        "value": -20
      },
      {
        "feature": "return_1m_bps",
        "op": "lte",
        "value": -2
      },
      {
        "feature": "range_position_20",
        "op": "gte",
        "value": 60
      },
      {
        "feature": "volume_ratio_5",
        "op": "gte",
        "value": 1.0
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
        "value": -15
      }
    ]
  }
}
```

## Results

| Period | Trades | Net USDT | Stress USDT | Notes |
|---|---|---|---|---|
| Pre-freeze study, 2026-09-14 to 2026-09-20, 2,015 separate 5-minute windows | 23 | -0.733 | not computed | Proxy built from public 1-minute candles (no real bid/ask), 100 USDT reset in every window. Cash (0) did better. |
| Same week, 335 separate 30-minute windows | 22 | -0.734 | not computed | Cash did better. None of the 10 pre-set one-parameter variations beat cash either. |
| DEV 2020-01 to 2026-10-02, official evaluator, exact engine | 16,979 | -674 | not reported | 2,465 one-day windows, results summed at about 30 USDT per trade. Negative in every year (2020: -126 ... 2026: -48). In 2024-2026: gross -3.5 bps and net -13.5 bps per trade; 9.5% of one-day windows positive, median day -0.12 USDT. |
| Live, five reviewed 24-hour rounds, 2026-11-08 to 2026-11-17 | 27 | -0.648 | n/a (real fills) | Sum of the per-round table below. Fees of 0.717 USDT against a gross result of +0.070 USDT. |

- **Gate K:** never submitted. The DEV net is negative, so the recipe does not meet gate K's first numeric condition (net > 0).
- **Gate D:** never submitted.

### Live rounds

Real money on OKX: 100 USDT per bot, isolated margin, 1x leverage, five bots per round, rules only (Jev in shadow mode). The project owner (kalambyrYura) started every round. The numbers come from the team reviewer's per-round reports, which recomputed gross, fees and funding from the exchange fills and bills.

| Round start (UTC) | Trades | Gross | Fees | Funding | Net USDT | Round status |
|---|---|---|---|---|---|---|
| 2026-11-08 21:15 | 8 | +0.17640 | -0.2098484 | -0.0012794 | -0.0347278 | complete |
| 2026-11-09 23:27 | 1 | -0.04368 | -0.02983316 | 0 | -0.07351316 | incomplete: data-feed and account-check failures; then a platform defect paused all bots for 16 h |
| 2026-11-11 23:42 | 2 | +0.10000 | -0.05751908 | 0 | +0.04248092 | complete; the only bot with a positive net that round |
| 2026-11-13 07:49 | 3 | -0.07128 | -0.08329664 | 0 | -0.15457664 | incomplete: a 126.9 s gap in observations; later a full disk on the host PC paused the round |
| 2026-11-16 01:01 | 13 | -0.09108 | -0.33675406 | 0 | -0.42783406 | complete |
| **Sum** | **27** | **+0.07036** | **-0.71725134** | **-0.0012794** | **-0.64817074** | |

From Sol's own review of the first four rounds (14 trades):

- 19 entry signals in 3.7 days, 5.1 per day: the same rate as on DEV in 2024-2026.
- 5 of the 19 IOC entries (26%) were not filled because the price moved above the limit first. The DEV model leaves 0.12% of entries unfilled. After the 5 missed entries the mid price moved -32.7, +0.1, -30.2, +45.4 and -32.0 bps over 15 minutes.
- Fills were slightly better than the model: entries 1.0 bps better than the decision ask, exits 1.4 bps better than the trigger bid. Fees were 10 bps per round trip.
- Exits: rule 10, time 3, take profit 1, stop 0. On DEV 2024-2026: rule 70.6%, time 20.0%, take profit 3.9%, stop 5.5%.
- The positive gross came from one take profit (+0.297 USDT). Without it, the gross was about -3.8 bps per trade, close to DEV's -3.5 bps.

Other Live rounds, not in the table:

- In a 5-hour Live round on 2026-10-02 the recipe made no trade (499 checks, 0 entries): its trend and pullback conditions never lined up.
- A sixth 24-hour round, at 3x leverage, started on 2026-11-17. It had not been reviewed when this card was written.

## Why it is not enough

- The move it catches is smaller than its costs. On DEV 2024-2026 the average trade made -3.5 bps before fees and -13.5 bps after them.
- It lost money in every DEV year, and only 9.5% of one-day windows in 2024-2026 were positive.
- Live was in line with DEV. Over 27 trades, fees of 0.717 USDT outweighed a gross gain of 0.070 USDT, and the small gross gain of the first four rounds rested on one take profit.
- The DEV model fills nearly every IOC entry, while Live left 26% unfilled, so DEV overstates the number of trades by about 1.35 times.
- Fixed bps thresholds do not adapt to volatility. Before freezing, the design was checked on one week of candles only, and that check already lost to cash.
- Short matches can end with no trade at all.

## If you want to continue

- Do not add leverage: the expected loss grows in step with it. Sol's estimate from DEV 2024-2026 is about -0.18 USDT per day at 1x, -0.54 at 3x and -1.8 at 10x.
- Sol saw no cheap fix: a maker (post-only) entry saves at most 3 bps, while the gross edge is about zero. First find a signal whose gross move is clearly larger than the round-trip cost. Sol's next line tried 12- to 24-hour holds with maker entries; see `sol-nc1-maker-pullback`.
- The V1 author proposed volatility-scaled rules (ATR or realized volatility, volatility-normalized stop, target and size) and nested conditions as extensions of the recipe format. None of them was tested on this recipe.
- Calibrate the evaluator's IOC fill model with Live fills before trusting DEV trade counts for fast entries.

## Reproduce

The V1 recipe format runs on the official evaluator, on its exact-engine path:

```
py -3.12 -X utf8 -B tools/team_eval.py run --recipe <recipe.json> --author sol --label v1-sol --set dev --store <data folder>/BTC-USDT-SWAP --out runs/v1-sol
```

- The original recipe file is not published (see "Exact rules"). A file rebuilt in the same format (`name`, `author`, `description`, `original_text`, `rules`, `confirmed`) with the `rules` block above runs the same rules, but its fingerprint will differ from `87facc50…`.
- The store must hold OKX BTC-USDT-SWAP 1-minute candles and funding history. Market data is not included in this repository; build the store with the repository's data downloader (see QUICKSTART).
- A whole-DEV run on the exact engine is heavy; the team ran it only under a booking with its coordinator.
