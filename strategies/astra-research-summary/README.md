# Astra · research lines after V1, including NC1

**Status:** Research summary, no candidate

- **Author:** Astra, an AI author session running on Claude Opus 5.5 (Astra is a fictional persona name). Work from 2026-10-28 to 2026-11-17.
- **Frozen:** none. No recipe from these lines was frozen or submitted to a gate.
- **Instruments:** OKX BTC-USDT-SWAP in most lines; ETH-USDT-SWAP in a transfer test, a descriptive follow-up and NC1; SOL-USDT-SWAP only as a control in NC1.
- **Side:** long and short, depending on the line.
- **Holding horizon:** hours to a day (mostly 4–24 hours; 48 hours only as a diagnostic). NC1: 8 or 17.5 hours.

## Idea

The first Astra recipe (V1, see its own card) lost to trading costs, so Astra looked for moves large enough to pay for them: hold for hours instead of minutes, and use information beyond one coin's recent price. A first census put the cost wall in numbers: a round trip costs about 14 bps (24 bps stressed), roughly 0.3 standard deviations of a 1-hour BTC move at recent volatility, so the target was at least 30–40 bps of gross move per trade, 0.3–2 trades a day and holds of 2–24 hours. Astra then tested one family at a time: higher-timeframe breakouts, time-of-day and calendar effects, US macro-release windows, multi-week momentum, the same rules on ETH, maker orders on wicks, leverage crowding (perpetual premium and settled funding, traded against the crowd), open-interest deleveraging, and finally NC1: fading the US-session move after the 16:00 ET fix of the spot-ETF net asset value. Each family was pre-registered with a fixed selection rule before its data was run, and its parameters were not re-tuned after a failure. By the author's own count, the lines used 72 configurations and 147 trial records. Astra spent $0 on Jev, the third-party model typesafe/jev-1.13 (reached through OpenRouter) that the project tried as an optional entry veto.

## Exact rules

No recipe was frozen. The families as tested:

| Family | Rule as tested |
|---|---|
| H1 breakout (BTC) | Long when a 1-hour close breaks above the high of the previous N hours (N = 24 or 72), short below the low; stop and trailing stop at K × ATR(1 h, 24), K = 2 or 4; time exit after 12 or 24 h |
| H2 session momentum (BTC) | At X:00 UTC (X = 8, 12 or 16), z = move since 00:00 UTC divided by (σ of 1-hour returns × √X); long if z > z0, short if z < −z0 (z0 = 0.5 or 1.0); exit at 24:00 UTC; 500 bps stop |
| H4 weekend reversal (BTC) | At Sunday 23:00 UTC, trade against the move since Friday 21:00 UTC if it exceeds 100 or 200 bps; hold 13 or 21 h |
| H7 and H7F macro windows (BTC) | On weekdays, follow the first 15-minute move in the 12:30 and 13:30 UTC windows (US 8:30 ET releases) when its z-score passes 1.5 or 2.5; H7F uses the Wednesday 18:00 and 19:00 UTC windows (FOMC hours); hold 4 or 8 h (H7F: 4 h); 500 bps stop |
| H7R (BTC) | Fade the same 15-minute release move instead; one variant adds the 14:00 and 15:00 UTC windows |
| H6 squeeze fade (BTC) | Short after a 1-hour bar at least 3 or 4 σ up; stop at 3 × ATR; hold 4 or 12 h |
| H8 multi-week momentum (BTC) | Daily at 00:00 UTC, side = sign of the 28- or 56-day return; H8A always in the market, H8B only after a day that closed against the trend; hold 23.5 h; 500 bps stop |
| Transfer (ETH) | Four rules selected on BTC, run unchanged on ETH |
| H11 maker on wicks (BTC) | Post-only limits 4 or 8 × ATR(1 m) away on both sides, maker target, taker stop at twice the distance, 60-minute time exit |
| H12X (BTC) | Fade a large 4-hour BTC move that ETH did not share; hold 4 or 12 h |
| H3P premium level (BTC) | At 00, 08 and 16 UTC, trade against the 24-hour average premium of the perpetual over the index when its z-score against the previous 90 days passes 1.5 or 2.5; hold 8 or 24 h; 500 bps stop |
| H3F funding level (BTC) | Trade against the average of the last three settled funding rates when its 90-day z-score passes 1.5 or 2.5; hold 24 h; 500 bps stop; actual funding counted |
| H4D open-interest deleveraging (BTC) | After a day when daily open interest fell by at least 1 or 1.5 σ and the price moved at least 0.5 or 1 σ, trade against the price move until the next day's decision; 500 bps stop |
| NC1 US-session fade (BTC, ETH) | Below |

**NC1, the last line.** Built as recipes for the project's current format and run on the official evaluator.

- **Idea.** US spot ETFs on BTC (from 2024-01-11) and ETH (from 2024-07-23) price their net asset value at 16:00 ET. Creations and redemptions are hedged during the US session, which pushes the price; after the fix that pressure stops, so part of the session move should revert overnight.
- **When.** Only in the last 5 minutes of the US cash session (`us_session_minute` ≥ 385), Monday to Friday, daylight saving time handled.
- **Entry.** Long if the move since 09:30 ET (`return_bps` over 385 one-minute bars) is at most −k × the typical hourly move (`rms_bar_bps` of 1-hour bars over 168 hours); short if it is at least +k × the same; k = 2.5 or 4.
- **Volatility filter.** `rms_bar_bps(1h, 24)` ≤ 2 × `rms_bar_bps(1h, 168)`: no volatility spike.
- **Order.** Post-only limit placed 0.25 × `rms_bar_bps(1h, 168)` away from the signal price on the passive side; it lives 1 hour, then is cancelled.
- **Exit.** 500 bps stop, no real target (3,000 bps), time exit after 8 or 17.5 hours; 1-hour cooldown; size 30%; one position.
- **Grid.** BTC and ETH × k 2.5 or 4 × 8 or 17.5 hours: 8 selectable configurations. Diagnostics, never candidates: FOLLOW (the same rule in the direction of the session move) and SOL (no US spot ETF for most of the period).
- **Selection rule.** On 2024-01-01 → 2025-06-30: net > 0, stressed net > 0, net > 0 even with all maker fills counted as taker, at least 40 trades, at least half of the active quarters positive. Only a selected configuration would get one look at the 2025-07 → 2026-10 tail.

## Results

Two kinds of numbers. The screens before NC1 ran on the author's own research simulator: mean **bps per trade**, after modelled costs of 14 bps per round trip (stress: 24 bps). NC1 ran on the project's official evaluator: **USDT** on a 100 USDT account, with stress adding 5 bps per execution. "A → B" shows one configuration on two periods: first the selection period, then the next check (a later period; for H3P and H3F, the required 2024-01 … 2025-06 part of the selection period).

| Line | Period | Trades | Net | Stress | Notes |
|---|---|---:|---:|---:|---|
| H1 breakout (8 configs) | 2020–2023 | 531–1,324 | −22.7 … −1.7 bps | −32.7 … −11.7 bps | All negative. Refuted |
| H2 Asian-session momentum (X08-Z10) | 2020–2023 → 2024-01 … 2025-06 | 243 → 85 | +32.5 → +6.9 bps | +22.5 → −3.2 bps | Failed validation. t 1.23 at discovery |
| H4 weekend reversal (4 configs) | 2020–2023 | 71–123 | −37.5 … +13.8 bps | best +3.8 bps | Best had 38% of quarters positive. Refuted |
| H7 drift after US releases (4 configs) | 2020–2023 | 128–288 | −64.1 … −45.3 bps | −74.1 … −55.3 bps | Prices reverted instead of drifting. Refuted |
| H7F FOMC hours (Z15-T4) | 2020–2023 → 2024-01 … 2025-06 → 2025-07 … 2026-10 | 62 → 30 → 18 | +17.6 → +25 → −11.2 bps | +7.6 → +15 → −21.2 bps | Failed the last look. Only about 16 trades a year |
| H7R fade of release moves (Z25-T4) | 2020–2023 → 2024-01 … 2025-06 | 128 → 52 | +17.5 → −18.3 bps | +7.5 → −28.3 bps | Failed validation |
| H6 squeeze fade (4 configs) | 2020–2023 | 105–284 | −26.5 … −20.6 bps | – | Upward spikes did not fade. Refuted |
| H8B 28-day trend, pullback entry | 2020–2023 → 2024-01 … 2025-06 | 650 → 252 | +26.4 → −10.2 bps | +16.4 → −20.2 bps | Failed validation; H8A too |
| Four BTC rules on ETH | 2020–2023 → later | – | X-H2 +34.0 → −3.8; X-H7R +36.3 → −50.6; X-H8B +17.9 → −8.4 bps | X-H8B +7.9 → −18.4 bps | 0 of 4 survived. X-H8B passed 2024-01 … 2025-06 (251 trades) and failed the 2025-07 … 2026-10 tail (190 trades) |
| H11 maker on wicks (4 configs) | 2020–2023 | 2,111–11,798 | −15.3 … −9.6 bps | −25.3 … −19.6 bps | Gross move negative even before costs |
| H12X fade of BTC's own move (4 configs) | 2024-01 … 2025-06 | 8–66 | gross −74.8 … −15.2 bps | – | Refuted |
| H3P premium level (Z15-T24) | 2020-01 … 2025-06 → 2024-01 … 2025-06 | 238 → 73 | +10.1 → −43.0 bps | +0.1 bps | Refuted at step 1; tail not opened |
| H3F funding level (Z15-T24) | 2022-03 … 2025-06 → 2024-01 … 2025-06 | 190 → 75 | +3.8 → −54.3 bps | −6.3 bps | Refuted at step 1; tail not opened |
| H4D open-interest deleveraging (4 configs) | 2024-01 … 2025-06 | 8–33 | −91.9 … −53.3 bps | −101.9 … −63.2 bps | At most 0.06 trades a day. Refuted |
| NC1 fade, BTC and ETH (8 configs) | 2024-01-01 … 2025-06-30 | 70–152 | −11.8 … −4.2 USDT | −14.0 … −6.8 USDT | All 8 negative, also with every maker-fill assumption. Refuted at selection; tail not opened |
| NC1, same 8, before the ETFs | 2020-01-02 … 2023-12-31 | 172–307 | −51.0 … −11.2 USDT | – | Also negative |
| NC1 diagnostic FOLLOW, BTC / ETH | 2024-01-01 … 2025-06-30 | 183 / 138 | −1.8 / −0.0 USDT | −6.6 / −4.0 USDT | Better than fading, still not positive |
| NC1 diagnostic SOL, 8 h / 17.5 h | 2024-01-01 … 2025-06-30 | 129 | −14.0 / −24.0 USDT | −17.7 / −27.7 USDT | Worst of all |
| ETH long side of H8B-L28 (descriptive, chosen afterwards) | 2020-01 … 2026-10 | 578 | +30.4 USDT | +13.1 USDT | t 1.11. The long-only split was picked after seeing the numbers, so it is a hypothesis, not evidence |

**Gate K:** no configuration reached it; every family failed its own pre-registered selection step first. The ETH long-side split was checked against the gate K rules only with the author's preliminary script and failed two of them: 5 of 10 active quarters positive in 2024+ (60% needed), and −7.8 USDT without the 10 best trades.

**Gate D:** no submission.

## Why it is not enough

- **Effects from 2020–2023 did not survive 2024+.** Asian-session momentum, the fade of US-release moves and multi-week momentum were all positive in 2020–2023 and failed in 2024-01 … 2025-06. The same rules on ETH failed the same way, so it was not a BTC-only change.
- **BTC is close to a random walk at these horizons.** Variance ratios of hourly returns stayed near 1 in every year from 2020 to 2026 (for example 0.86–1.08 at 24 hours), so price history alone carries little to trade on.
- **Positioning signals turned into market drift.** Trading against an extreme premium or funding level made money in 2022–2023 but lost from 2024. In 2024 to mid-2025 only the long side earned (premium: +52 bps long against −106 bps short per trade), and the pro-crowd control showed the same split, so the profit was the market rising, not information.
- **Open-interest data was too sparse.** Daily history only from 2024, and even the loosest thresholds gave 0.06 trades a day, about 33 trades in 18 months, far below the 100 trades the gate needs. After long-deleveraging days the price kept falling.
- **Maker orders do not create an edge.** Limit orders deep in the book filled mostly in moves that kept going; the gross result was negative before costs.
- **NC1 moves were big enough; the direction was not.** The median absolute gross move per NC1 trade was 85–245 bps, many times the cost, yet every configuration lost. The session move tended to continue rather than revert, without being tradable in either direction.
- **Weak statistics.** The best early numbers had t-values around 1 after dozens of trials, which is what noise looks like.

## If you want to continue

- **Demand both eras.** Treat 2024+ as its own regime: require a positive result on 2020–2023 and on 2024+ before touching any holdout.
- **The strongest lead is post hoc.** The ETH long-only "pullback in a 28-day uptrend" (long when the 28-day return is positive and the last day closed down, hold 23.5 h, 500 bps stop) was positive on all three periods, but it was chosen after seeing the data and has t 1.11. Only a fresh forward test can tell.
- **NC1 points the other way.** Following the US-session move did better than fading it, but still not above zero. Any "follow the session" rule needs its own pre-registration on new data.
- **Faster positioning data must be collected forward.** The exchange API gives daily open interest from 2024 and only the last 1,440 records for shorter periods, so intraday open-interest and liquidation signals can only be tested on data recorded from now on.
- **Cross-asset lead-lag needs an engine change.** The current recipe format cannot read another asset, and a teammate's test found that altcoins followed a BTC burst by only +2 to +13 bps over the next 60 minutes (t ≤ 1.4).

## Reproduce

No recipe was frozen, so there is no official evaluator command for this card. The 12 NC1 configurations ran on the project's official evaluator (`team_eval` v4, execution model `v4-path5s-2`) on the development data only. The earlier screens ran on the author's research simulator, which matched the official engine within +0.46 bps per trade on average on 59 test windows (correlation 0.9999); it is not the official evaluator.
