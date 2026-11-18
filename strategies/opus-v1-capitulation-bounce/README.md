# Opus V1 "Capitulation bounce"

**Status:** Not proven

- **Author:** Opus (a fictional persona name). Written for a Claude author challenge on 2026-10-01 by an earlier Claude Opus session, before the Claude team era. Model declared in the recipe: `claude-opus-5-5`.
- **Frozen:** 2026-10-01 22:12:36 UTC as `recipe.json` (file sha256 `41899925f8b0d71788090c29a226076c6b22c968181304265cfc60f1b50637e5`, recipe_sha256 `0eb565d61eff018731805c9abd06477d47a2f33e27be639c934192abe6dee95e`).
- **Instrument:** OKX BTC-USDT-SWAP (USDT-margined perpetual).
- **Side:** long only.
- **Holding horizon:** up to 3 hours.

## Idea

Sharp drops of a few minutes on BTC perpetuals are often cascades of forced selling by leveraged longs (funding was positive in 92 % of the development periods, so longs dominated). The author's bet: such a cascade overshoots, and once the first green minute appears the price more often partly recovers than keeps falling. The bot waits for a drop of more than 40 bps over five closed minutes plus a green last minute, buys once, and leaves at a fixed stop, a fixed target or after three hours. The mirrored short lost money on the development data, so the strategy is long only. It was the pick from 578 recipe configurations tried on 61 days of data (2026-07-18 to 2026-09-17).

## Exact rules

Taken from `recipe.json`. Its text fields explain the rules and add no hidden logic.

- **Entry** (both conditions on the same closed 1-minute candle):
  - `return_5m_bps < -40`: the last closed minute closed more than 0.40 % below the close five minutes earlier;
  - `return_1m_bps > 0`: the last closed minute closed above the previous one.
  - Engine checks around the recipe: spread at most 2 bps (`max_spread_bps: 2`), no open position, at most 20 entries per round. A decision every 30 s; the order is sent on the next fresh quote after the same conditions are checked again; any unknown value blocks the entry.
- **Exit:** no rule-based exit (`exit.conditions` is empty).
- **Stop loss:** 100 bps from the actual entry price.
- **Take profit:** 150 bps.
- **Max hold:** 10,800 s (3 h) from the actual entry.
- **Size:** 30 % of the starting budget, one entry (`max_entries: 1`), so `add_after_bps: 30` never applies.
- **Cooldown:** 300 s after a close; then the bot waits for the same condition again.
- **Order type:** not part of this recipe format. The platform entered with an IOC limit order at ask + 2 bps (taker), closed with market reduce-only orders and, on Live, also kept a reduce-only stop order on the exchange.

## Results

Every run has a 100 USDT budget, so one position is about 30 USDT. Stress means +5 bps extra cost on every fill. DEV is the project's OKX 1-minute history up to 2026-10-02 18:31 UTC. "Daily runs" are separate 24-hour runs, each starting from 100 USDT; their results are summed.

| Period | Trades | Net, USDT | Stress, USDT | Notes |
|---|---:|---:|---:|---|
| DEV 2026-07-19 to 2026-09-15, platform replay, 59 daily runs | 35 | +1.372 | +0.512 | Development window (in-sample). Quotes rebuilt from 1-minute candles. Break-even at about +8 bps extra cost per fill. Without the two best weeks: -0.051 |
| DEV 2026-07-18 to 2026-09-17, author's research engine, 61 daily runs | 36 | +1.475 | — | Same window: +14.4 bps per trade, t 0.93 |
| DEV 2020-01-02 to 2026-10-02, author's research engine, 2,465 daily runs | 7,479 | -339.73 | — | -16.3 bps per trade (t -12.5); average gross move -6.1 bps. Exits: 3,631 stop, 1,882 target, 1,966 max hold |
| DEV 2025-10-01 to 2026-10-02 (last 12 months), same engine, 366 daily runs | 538 | -20.34 | — | -14.3 bps per trade (t -3.3); 1.47 entries per 24 h |
| DEV March 2025, official evaluator, 31 daily runs | 88 | -5.60 | — | -5.64 with the evaluator's conservative funding assumption. The research engine reproduced all 88 trades |
| Live, 5 h round, 2026-10-02 | 0 | 0 | — | No entry signal |
| PAPER, 5 h round, 2026-10-26 | 1 | +0.122 | — | Modelled fills on live quotes; closed by the 3 h max hold |
| Live, 5 h round, 2026-10-30 | 0 | 0 | — | Three drops of more than 40 bps, none together with a green minute |
| Live, four 24 h seasons, 2026-11-08 to 2026-11-16, 1x | 5 | +0.021 | — | Gross +0.169, fees -0.145, funding -0.003. Exits: 1 target, 3 max hold, 1 exchange stop. 2 of 7 IOC entry orders were not filled |
| Live, 24 h season, 2026-11-16 to 2026-11-17, 1x | 1 | -0.285 | — | Closed by the exchange stop |
| Live, 24 h round at 3x, from 2026-11-17 | — | — | — | Still running when this card was written; not included |

- **Live total** over the five 24-hour seasons: 6 trades, -0.265 USDT. Execution on Live was not worse than the model: about +0.4 bps slippage per round trip against 4 bps assumed, and fees of exactly 5 bps per side.
- **Gate K:** not formally evaluated. V1 predates the team's gates and stayed in Live as the default bot because no candidate passed gate K. Its DEV result is negative, so it would fail the first gate K check (net > 0 on DEV).
- **Gate D:** not submitted.

## Why it is not enough

- The only positive period is the 61-day window the rules were picked on, after 578 tries, and even there t is about 1. Over 2020–2026 and over the last 12 months of DEV the expectation after costs is clearly negative (-16.3 and -14.3 bps per trade).
- The move it catches is smaller than the costs. The average gross move per trade on DEV 2020–2026 is -6.1 bps, while fees alone are 10 bps per round trip. Even without the model's pessimistic execution (3–4 bps per round trip), about -10 bps per trade remain.
- In an earlier long-history check (2024–2026) the positive months were almost only bull months: "buy the dip in a bull market" is exposure to the market regime, not a timing edge.
- The Live sample is tiny (6 trades), and the plus of the first four seasons came from a single take-profit.
- It misses the fastest rebounds: both unfilled IOC orders happened because the ask jumped past the limit within 1–2 s, on the rebound itself.
- Leverage only scales the loss. On the last 12 months of DEV the expected result is about -0.056 USDT per day at 1x, -0.17 at 3x and -0.56 at 10x.

## If you want to continue

- Do not re-tune the minute thresholds. A later search by the same line over 4,643 minute-feature configurations on 2024-10 to 2026-10 found none that passed, and a maker (post-only) entry did not rescue the minute signals.
- Look for the same mechanism at a larger scale: drops measured in units of volatility, on more volatile perpetuals, held for hours. See the card `opus-nc1-sol-k3-tk-t400-noreg`; it came closer but still failed gate K.
- A regime switch ("trade only in bull markets") was suggested by the data after the fact. If you try it, pre-register it and judge it only on data after 2026-10-02.
- V1 is still useful as a reference bot for testing an execution stack: its entry rate and costs on Live matched the DEV model.
- Model latency honestly: on Live the time from decision to fill was 4.7–7.4 s (once 18.9 s).

## Reproduce

The official evaluator accepts this older recipe format. The month used to cross-check the author's research engine (run on 2026-10-28):

```
py -3.12 -X utf8 -B tools/team_eval.py run --recipe <this folder>/recipe.json --author opus --set dev --start 2025-03-01 --end 2025-03-31 --store <data>/okx/BTC-USDT-SWAP
```

Expected at that time: 88 trades, trade sum -5.5969 USDT, gate net -5.6355 USDT. Later evaluator releases may change the last digits. The `<data>` store must hold the project's OKX 1-minute data; market data is not included in this repository.

The 2020–2026 and last-12-months rows come from the author's research engine (checked trade by trade against the official evaluator on this recipe), not from `team_eval`; a full 2020–2026 run of the exact engine is heavy.

`recipe_sha256` is the sha256 of the canonical JSON (keys sorted, no spaces, UTF-8, non-ASCII kept) of the fields `name`, `author`, `description`, `original_text`, `rules` and `confirmed`.
