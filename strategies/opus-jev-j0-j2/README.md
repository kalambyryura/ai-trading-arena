# Jev as a numeric filter, a veto and a news reader (J0–J2)

**Status:** Research summary, no candidate

- **Author:** Opus (a fictional persona name). J0–J2 were run by a Claude Opus 5.5 session in the Claude team era, 2026-10-28 to 2026-10-31. The background experiments E0–E4 (2026-10-03) come from the earlier Opus line, a Claude Opus session with the model declared as `claude-opus-5-5`.
- **Frozen:** no recipe. Every paid step had a pre-registration committed before its first call, and the J0 analysis code was committed before any answer arrived.
- **Instrument:** BTC-USDT-SWAP (numeric contexts and the outcome measure).
- **Side:** none. Jev could only block an entry; it never opened, sized or closed a trade.
- **Holding horizon:** not applicable. The J2 veto covered the next hour; its planned outcome measure was BTC volatility over the next 4 hours.

**What Jev is.** The third-party model `typesafe/jev-1.13`, called through OpenRouter's decisions endpoint. It answers only typed questions (pick one option, a score, or yes/no) and returns a confidence. In this project it was an optional entry veto: the arena could put a shared Jev gate in front of entries (used in the early Live rounds), while the recipes themselves contain no Jev logic and the later Live rounds ran rules-only. All J2 answers came from one model snapshot, `typesafe/jev-1.13-20260917`.

## Idea

Can a language model add anything to a rule-based crypto strategy? Three uses were tested:

1. **Numeric filter:** show the model the market numbers the code already sees and ask about the next move or the risk of an entry.
2. **Veto:** let the model block the code's entries when the market state looks abnormal.
3. **News reader:** give it the headlines of the last hour, which the code cannot read, and ask whether there is a market shock; a shock would block new entries for an hour.

Design rules: typed questions with a safe default (any error means HOLD); anonymised numbers (no prices, dates, times or asset names) with a mirror test, a shuffled-data placebo and a memory-leak test; news only forward in time, after the model's release, never historical text.

## Exact rules

There is no trading recipe; these are the experiment protocols.

- **Shared entry gate, E0–E3 (earlier line):** in the arena, a candidate entry went to Jev with the recipe's passed conditions and market features, and a confidence of at least 0.70 was needed to trade. E0–E3 replayed and varied these requests.
- **E4, numeric filter (earlier line):** four yes/no questions (rebound starting, selling exhausted, trend still strongly down, higher price in 60 minutes) on 14 market features, for every V1 signal of a continuous simulation: TRAIN 2024-10 to 2025-09 (502 signals), VALID 2025-10 to 2026-07 (483 signals). Pass rule fixed before the calls: a filter with t >= 2 that beats the best code filter and keeps the remaining trades above 0.
- **J0, calibration:** 50 BTC contexts ending between 2021-03-01 and 2023-12-31 (25 random moments, 25 after a 24-hour move of at least 2 sigma). 38 features: z-scores of the last 24 hourly and 7 daily returns, cumulative returns, position in the 72-hour range, drawdown from the 30-day high and volume ratios, rounded to 0.1. Each context in five variants: original, mirror (all signs flipped), placebo (bars shuffled) and two repeats with fresh noise; plus 10 short contexts to measure cost; 260 calls in total. Questions: `next_move` {higher, lower, similar}, `state` {orderly_trend, forced_cascade, range, volatility_burst}, `entry_risk` {normal, abnormal}. Pass rules: repeats agree in at least 80 % of cases; `state` unchanged under the mirror in at least 70 %; placebo answers differ from the originals (chi-square p < 0.05); no memory leak (accuracy on originals not 10+ points above the mirrors).
- **J1** (the state classifier as a veto on historical signals) was allowed only if J0 passed.
- **J2, news reader (forward only):** every hour at HH:05 UTC from 2026-10-28 23:05, up to 12 headlines published in the previous 60 minutes (titles only, no links) from the platform's headline recorder. Questions: `shock` {none, minor, major}, `scope`, `direction` {risk_off, risk_on, unclear}, `veto` {allow, wait}. An hour without headlines is labelled `no_news` by code, without a call; no hour was picked by content. The verdict belonged to the team's Judge and needed at least 500 news hours and at least 10 `major` hours: a confidence-weighted shock score had to rank-correlate with BTC volatility over the next 4 hours (normalised by the same hour's 30-day median), beat two code baselines (headline count, a fixed shock-keyword count) and a placebo (the same hour one day earlier); a usable veto had to say `wait` in at most 10 % of hours.
- **J3** (a checklist on V1 entry signals, shadow only) needed at least 60 signals.

## Results

| Experiment and period | Paid calls | Cost, USD | Pre-registered verdict | What it showed |
|---|---:|---:|---|---|
| E0–E3, 2026-10-03, shared entry gate | 138 + 36 | 0.0077 + 0.0017 | — | Confidence followed the number of recipe conditions, not the market: mean 0.663 with 1 condition (23 % reached 0.70), 0.775 with 2, 0.847 with 5. Repeats of the same request differed by up to 0.10. In the Live round of 2026-10-02 the gate blocked all 5 entries of another author's one-condition dip-buying recipe (confidence 0.60–0.65). With the conditions hidden it became a near-constant 0.45–0.66 that blocks everything |
| E4, 2026-10-03, numeric filter on 985 V1 signals | 986 | 0.0384 | FAIL | Correlation of the answers with trade results: -0.113 to +0.059 on TRAIN, +0.003 to +0.061 on VALID. A filter chosen on TRAIN reversed on VALID: kept trades -23.0 bps, rejected trades -12.1 bps |
| J0, 2026-10-28, anonymised numeric contexts | 260 | 0.0115 | FAIL (symmetry, structure) | Near-constant answers: `entry_risk = abnormal` in 149 of 150 originals, mirrors and placebos; `next_move = lower` in about 80 %; `orderly_trend` never chosen. The mirror kept the same `state` in only 38 % (70 % needed). Placebo against original: chi-square p 0.052. Repeats agreed (87 % and 99 %). No memory leak detected (accuracy 0.26 on originals against 0.30 on mirrors, p 0.67; a weak test). Median cost $0.0000446 per call |
| J1 | 0 | 0 | Cancelled by rule | J0's structure check failed. Afterwards, without new calls: the confidence of `abnormal` tracked the size of the recent move (correlation 0.14–0.25) just as much on the shuffled placebos, so a veto would only copy a code feature with noise |
| J2, decision hours 2026-10-28 23:05 to 2026-10-31 01:05 UTC, hourly headlines | 31 | 0.0012 | None: 31 labelled hours of the 500 needed | 51 grid hours: 31 labelled, 4 without news, 16 refused because the headline recorder had gaps. Labels: `shock` major 15, minor 12, none 4 (not degenerate); `direction` risk_off 19, risk_on 6, unclear 6; `veto = wait` in 26 of 31 hours (84 %; a usable veto needs 10 % or less). The labels were never compared with the market |
| J3 | 0 | 0 | Not started | V1 produced no entry signal in the 5-hour Live round of 2026-10-30; at DEV rates, 60 signals would need 43 to 293 such rounds |

Total paid Jev cost of the team-era Opus line: $0.01263 for 291 calls (J0 and J2). The earlier line spent $0.0478 on 1,160 calls (E0–E4). Gate K and gate D do not apply: there is no recipe.

## Why it is not enough

- **Numbers the code already has:** no information beyond the code (E4), near-constant answers (J0), and a confidence that follows the size of the move even on shuffled data. An LLM veto on such inputs is a noisy copy of a simple code rule.
- **As an entry gate:** it measured how many conditions a recipe listed, not the market, and it was random near the 0.70 threshold. Simple recipes were blocked whatever the market did.
- **As a news reader:** far too few hours (31 of at least 500), 16 of 51 hours lost to recorder gaps, and a veto that said `wait` almost all the time. The model's training cutoff and the absence of web access during calls were never officially confirmed. J2 therefore gives no evidence for or against the value of headline labels.
- Money was not the limit: a call cost about $0.00004–0.00005. Time, an uninterrupted recorder and a long enough forward sample were.

## If you want to continue

- Reuse the J2 protocol as it is: a fixed hourly grid, code baselines (headline count, keyword count), a placebo from the previous day, and decision rules fixed before any call. Run the recorder without gaps for the planned six weeks (about 1,000 calls, roughly $0.05–0.10 at J0 prices).
- Calibrate the veto on a first period: use the confidence-weighted shock score, fix the threshold so that at most 10 % of hours are vetoed, then freeze it.
- Confirm the model's training cutoff and that calls have no web access, and record the model snapshot of every answer.
- Do not ask an LLM about numbers the code already computes; give it information the code cannot read (text, events).
- Judge any veto by its effect on a base strategy in shadow mode (losses avoided against profits missed), with at least 60 base signals, not by its labels alone.

## Reproduce

There is no official-evaluator command: these are paid calls to a third-party model. The J0 contexts were built offline from BTC 1-minute candles by the author's case generator (`j0_cases.py`, seed 20261029, questions in `jev_questions_j0.json`); the analysis (`j0_analyze.py`) was committed before the first answer. These author scripts are not in this repository. The platform's research harness (`tools/jev_research.py`) has a `--dry-run` mode that builds the exact request bodies without network access. Repeating the calls needs your own OpenRouter account; J0 cost about $0.0115 for 260 calls. J2 needs a forward headline recorder; the recorded headlines are not part of this card.
