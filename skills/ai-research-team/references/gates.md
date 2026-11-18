# Gates

A gate is a fixed test a result must pass before it gets a stronger label. Gates exist because good-looking backtests are cheap: costs eat small edges, every extra configuration raises the chance of a lucky winner, a rising market makes almost any long rule look smart, and one short live run is noise. The thresholds below are the reference team's; adapt them, but fix them before the first submission and never relax them afterwards.

## Contents

1. Status ladder
2. The accounting model
3. Gate K: the candidate gate on development data
4. Gate D: the proof gate on unseen data
5. Special cases: rare outcomes and long horizons
6. Calibrate your gates
7. Adapting gates to another domain
8. What the gates said in the reference project

## 1. Status ladder

| Status | Meaning | Set by |
|---|---|---|
| DRAFT | idea under research | author |
| CANDIDATE | passed gate K on development data | author submits, coordinator reproduces |
| TEST-LIVE | candidate run by the owner at minimum size, labelled "test" | owner |
| PROVEN | passed gate D on sealed and forward data | judge |
| CHAMPION | proven, and the cumulative live result is above zero over ≥ 4 owner runs of 24 h or more | coordinator, from live data |

A demo, a single round or a backtest on development data is never presented as proof of profitability. If a participant's new version fails, its previous best version stays.

## 2. The accounting model

- Costs on every action: fee and slippage on every fill, a modelled spread, funding (or carry). The reference team charged 5 bps fee plus 2 bps slippage per fill, a one-tick spread and real funding, with a conservative charge where funding history was missing.
- **Stress:** the same trades with an extra cost on every fill (+5 bps), without re-simulating. A result must survive base costs and stress.
- One account per strategy (100 USDT in the reference team), one net position at a time, no leverage inside the gate model.
- Features use closed bars only; an order reaches the book at the next quote; if a stop and a target could both trigger in the same minute, the stop wins.
- A window that fails because of a data or engine error fails the gate until the cause is fixed. Windows are never skipped.
- Before any verdict, the evaluator passes causality tests (poison future data at hundreds of moments and check that nothing changes) and a review by a session that did not write it. The judge reviews it again for look-ahead before the first gate D verdict.

## 3. Gate K: the candidate gate on development data

Official evaluator only. Every check must pass:

| # | Check | Reference threshold |
|---|---|---|
| K1 | Form | the recipe is in the shared format and passes validate and lint |
| K2 | Whole DEV | net > 0 and stress > 0 |
| K3 | Size and worst year | ≥ 100 closed trades; no calendar year below −10 % of capital |
| K4 | Modern epoch | net > 0 and stress > 0; ≥ 40 closed trades; ≥ 6 active quarters, ≥ 60 % of them positive |
| K5 | Tail | ≥ 15 closed trades and net > 0 at base costs (stress reported) |
| K6 | Old epoch | under stress not below −5 % of capital |
| K7 | Not a few trades | net without the 10 best trades ≥ 0 |
| K8 | Paper trail | a PREREG made before the test and a complete TRIALS log with the number of configurations tried |

- A quarter is active with at least 5 closed trades; quarters with fewer are left out of the share but count in the sums. The quarter checks test the stability of one fixed recipe, not a re-fit per quarter.
- An epoch covered by less than 95 % is NOT_EVALUATED, and the verdict is NOT_ALL_CHECKS_EVALUATED, not PASS.
- Gate K is a necessary filter, not evidence of an edge. The reference team's only candidate passed with 7 of 11 positive modern quarters; one more negative quarter (6 of 11, 54.5 %) would have failed it. Its modern-epoch result was about 2.4 % a year.

## 4. Gate D: the proof gate on unseen data

Only the judge runs it.

| # | Check | Reference threshold |
|---|---|---|
| D1 | Submission | a frozen recipe identified by sha256; at most 2 submissions per author in 14 days |
| D2 | Result | official evaluator, same costs and stress: net > 0 and stress > 0 |
| D3 | Sample size | declared N ≥ 20 closed trades (the reference candidates declared 30) |
| D4 | Concentration and risk | largest trade ≤ 30 % of gross profit; max account drawdown, open position included, ≤ 10 % of capital |
| D5 | Live | ≥ 2 owner live runs of 24 h or more without execution anomalies, each with a fill of this recipe; actual costs no worse than the model by more than 3 bps |
| D6 | Reporting | PASS / FAIL / INSUFFICIENT plus aggregates only; authors never see per-trade details of the sample |

**Rules that keep the verdict independent of how often anyone looks:**

- **One run per recipe sha256** per re-evaluation and per execution model.
- **Declared N.** The author declares N at submission. The verdict is computed on the first N closed trades of the sample and is final for that sha256; until then the answer is INSUFFICIENT. Trades closed only by the end of the data are not closed trades; they wait for their real exit.
- **Sample start:** the later of the freeze (the recipe hash appears in `main`) and the submission merge, taken from the coordinator's merge commits, never from the author's own timestamps. Data the author saw after freezing (for example live packets) moves the start to the submission.
- **Worse rule:** if the execution model changes before the verdict, compute under every model in force since the submission and keep the worse result. After a verdict a model change can only make it worse: PASS can become FAIL, never the reverse. A code change that keeps the execution model is accepted only if the same trades come out.
- **A changed recipe is a new submission,** judged only on data after its own freeze, and it counts against the submission limit.
- **Write down what a result would mean before it exists.** For a recipe that failed gate K but was submitted anyway, the reference lead fixed in advance: a PASS on the first 30 trades means "forward pass, not proven"; PROVEN needs a second, non-overlapping window of 30 trades; a FAIL in any window removes it from live.
- **Report the strength of evidence** with every PASS or FAIL: how often a no-edge version of the same trade distribution (a bootstrap of the author's development trades shifted to zero mean) passes the numeric checks. It does not change the status; it tells the reader what N trades can mean.

## 5. Special cases: rare outcomes and long horizons

A strategy that holds positions for days or weeks makes 4–15 trades a year per instrument: it can never reach 100 trades, and "without the 10 best trades" works against how trend strategies earn. Simply lowering the minimums lets market growth through: a synthetic strategy that buys on random days and holds for weeks passed net and stress on the reference DEV in 88–98 % of cases.

The adapted track the reference judge wrote (K-T) asks a different question: are these entry and exit dates better than the same trades at other dates?

- Daily exposure: the signed entry notional over capital while a position is open (sampled at noon UTC), zero otherwise.
- S(0) is the sum of exposure × daily result over the real dates; S(k) is the same with the whole exposure pattern moved k days later, wrapping around, for every k. Every shifted copy has the same trades, lengths, sides and pauses; only the dates differ.
- p = (1 + number of shifts with S(k) ≥ S(0)) / T. Buy-and-hold gets p near 0.5, costs do not matter, and the test holds its level for small thresholds.
- Correct for choosing the best of m configurations: Bonferroni (p ≤ 0.05 / m over every configuration declared in the PREREG), or an exact family test that shifts all declared configurations together and compares standardized scores.
- Add checks on the modern epoch (timing p ≤ 0.10) and the tail (p ≤ 0.50), minimum counts, no collapsed years, and "without the 2 best trades" instead of 10.

Rules for any adapted gate: the author proposes it through the coordinator before the runs; only the judge adopts it; it must separate timing skill from market drift and correct for picking the best of m. An official verdict also needs evaluator support for the strategy's holding times; in the reference project that extension was planned but never built, so the track never produced a verdict.

For long horizons there is no fast honest forward proof: at a few trades a year, 20 forward trades take years. Say so up front.

## 6. Calibrate your gates

Before trusting a gate, measure how often it is fooled. The reference judge did this on synthetic strategies over real development prices, with an edge injected by an "oracle with noise" (no real rule was run on development data to set thresholds):

| Edge | passes K-T, one configuration | best of 24 |
|---|---|---|
| none | 1.1 % | 1.5 % |
| weak | 19 % | 27 % |
| medium | 45 % | 51 % |
| strong | 81 % | 78 % |

- A weak real edge passes only 8–29 % of the time, depending on how often it trades. With 6.75 years of history and 25–70 trades in total, a weak edge cannot be told apart from luck; that is a limit of the data, not of the rule.
- Gate D at N = 30 is weak evidence too: for the reference candidate, a no-edge version of its trade distribution passed the numeric gate D checks about 43 % of the time, against 64 % with the recorded edge. A PASS would raise the odds of a real edge only about 1.5 times. Going from 30 to 40 trades added little (64 % to 67 %) for about 50 more days of waiting.
- Test that trend rules on artificial prices without any predictable direction (shuffled daily changes) do not pass more often than the nominal rate.

Calibration tells the owner what a PASS is worth before anyone hopes for one.

## 7. Adapting gates to another domain

- Replace "net after costs" with your success metric under realistic costs (compute, latency, human review time, fees), and "stress" with the same metric under pessimistic costs.
- Replace "closed trades" with independent outcomes, and set the minimum count from a calibration, not from intuition.
- Keep the structure: a candidate gate on development data with stability checks (by epoch, by subgroup, without the best few outcomes), a proof gate on data that did not exist when the method was frozen, a declared sample size, a frozen hash, one run per hash, aggregates only, thresholds fixed before the first submission.
- Keep "worse rule" thinking for any change in the evaluation pipeline after submission.

## 8. What the gates said in the reference project

- Gate K: one strategy passed (a post-only burst entry on ETH, held up to 24 hours), with the thin margin above. A second recipe failed gate K on one check (6 of 11 positive modern quarters) and was still submitted to gate D, labelled "not proven" in advance.
- Gate D: INSUFFICIENT for both when the project stopped: 1 of 30 declared trades for the first, 0 of 30 for the second, whose forward sample started on 17 Nov 2026. No live round counted toward D5: the candidate had no fill in the seven 24-hour rounds it took part in.
- No strategy reached PROVEN. The gates did their job: they kept "almost" from being reported as "works".
