# Methodology: how the strategies were tested

This project asked whether autonomous trading bots could earn money after costs on crypto perpetual futures (OKX USDT-margined swaps on BTC, ETH and SOL). It was archived on 2026-11-17. This document explains the tests every strategy had to pass (the team called them gates), how reliable those tests are when checked on synthetic data, the rules that kept the team from fooling itself, and what the gates said about the last strategies.

## Summary

- No strategy was shown to have an edge. No strategy passed gate D, the test on market data its author never saw.
- One strategy passed gate K, the test on historical data: H10-ETH. When the project stopped, its gate D sample contained 1 of the 30 closed trades it needed, so its verdict was INSUFFICIENT.
- A second strategy, SOL-NC1-K6-F0-EK, failed gate K on one check. It was sent to gate D anyway, as a forward-only test labelled "not proved". Its gate D sample contained 0 of 30 trades.
- The gates are built to reject results that come from luck, from market drift or from trying many variants. On synthetic data, a long-horizon strategy without an edge passes the long-horizon version of gate K in about 1-2% of cases. A weak real edge (one configuration) passes only 8-29% of the time, depending on how often it trades.
- The tools that compute the gates are in this repository and run on public OKX market data. Section 10 shows how.

## Contents

1. Why the gates exist
2. Data, periods and roles
3. The accounting model shared by all gates
4. Gate K: the historical test
5. Track K-T: gate K for long holding periods
6. Gate D: the forward test
7. Calibration: how often the gates are fooled
8. Rules against overfitting
9. Verdicts when the project was archived
10. Running the gate tools yourself
11. Known limits
12. Glossary

## 1. Why the gates exist

A backtest that makes money is easy to find and usually means little. The project's own history showed this several times:

- Before the team started, the project compared 944 one-day historical runs of 16 strategy recipes (59 days, 2026-07-19 to 2026-09-15). 13 of the 16 recipes lost money after costs, and 12 of 16 lost money even before explicit fees. The only recipe that passed a first filter made +0.023 USDT per run on a 100 USDT account; it turned negative without its two best weeks, or with 8 bps more cost per execution.
- On 2024-2026 history, all four first-version (V1) recipes, which served as baselines, had negative expectancy: -9 to -20 bps per trade.
- Costs form a wall. A round trip costs about 14 bps (two executions at a 5 bps fee plus 2 bps slippage) plus the spread, while signals on 1-minute data typically moved a few bps.
- Searching harder is not enough. An earlier search over about 4,700 configurations of 1-minute features found nothing. Every extra configuration raises the chance that one of them looks good by luck.
- A profit can come from the market, not from the rule. After 2024, in several research lines only the long side made money. Over the selection period used for long-horizon research, buying and holding multiplied BTC by about 15, ETH by about 19 and SOL by about 51.
- A single five-hour Live round is noise. Evidence has to come from many trades.

So the gates ask three questions, in order:

1. Gate K: does the rule survive realistic costs on the full development history, in the modern market, and without its best trades?
2. Gate D: does the frozen rule still work on market data its author never saw?
3. Live: does real execution on the exchange match the cost model?

The letters come from the team's Ukrainian documents: K for "kandydat" (candidate), D for "dovedena" (proved).

A strategy moves through these statuses:

| Status | Meaning | Set by |
|---|---|---|
| DRAFT | idea under research | author |
| CANDIDATE | passed gate K on development data | author submits, coordinator reproduces |
| TEST-LIVE | a CANDIDATE trading one minimum lot, labelled "test", in the owner's real rounds | owner |
| PROVED | passed gate D on held-out and forward data | Judge |
| CHAMPION | PROVED, and a total Live result above zero over at least four rounds of 24 hours or more | coordinator, from Live data |

The thresholds were set before the first submission. The Judge could tighten them only before then; afterwards they were fixed. The Judge confirmed them without relaxing any. The team's rules also said that a demo, a single round or a backtest on development data must never be presented as proof of profitability.

## 2. Data, periods and roles

### Data

1-minute candles and funding history of the OKX perpetual swaps BTC-USDT-SWAP, ETH-USDT-SWAP and SOL-USDT-SWAP, taken from OKX's public endpoints (no account or keys needed). BTC and ETH data start on 2020-01-01; SOL starts on 2021-01-22, when its swap began trading. Index and mark-price candles were collected too, for features that need them.

### Who could see what

| Set | Period (UTC) | Who could see it | Used for |
|---|---|---|---|
| DEV | 2020-01-01 to 2026-10-02 18:31 | everyone | research and gate K |
| SEALED-1 | 2026-10-02 18:32 to 2026-10-28 00:00 | the Judge only (the platform downloaded it and recorded hashes) | gate D |
| FORWARD | after 2026-10-28 00:00 | the Judge; authors only through sanitized packets from Live rounds | gate D |
| LIVE | the owner's real trading rounds | everyone, as sanitized packets | execution calibration |

Authors were not allowed to download or open market data after the DEV cutoff on their own.

### Periods inside DEV

- E1, the old epoch: 2020-01-01 to 2023-12-31.
- E2, the modern epoch: 2024-01-01 to 2026-10-02 (11 full quarters, 2024Q1 to 2026Q3).
- The tail: the last five quarters of DEV, 2025-07-01 to 2026-10-02.

The split reflects the team's view that the market changed after spot ETFs arrived in 2024 and that effects from 2020-2023 no longer held. A rule has to work in E2 and in its tail; in E1 it only has to avoid large losses.

### Roles

- **Authors** research and submit strategies. From 2026-10-28 there were four AI author sessions with the fictional names Astra, Sol, Sonnet and Opus, all running on Claude Opus 5.5. "Sonnet" and "Opus" are persona names, not model names. A fifth slot, MYHuman, was the owner's own strategy slot; its recipes were developed with an AI assistant session. The older V1 baseline recipes came from earlier chats: Astra's and Sol's were written on 2026-09-30 by two chats labelled gpt-6-astra and gpt-6.1-sol, Opus's and Sonnet's on 2026-10-01/02 by Claude Opus and Claude Sonnet chats (model names as declared, not verified).
- **The coordinator** reproduces every gate K result with the official evaluator before a strategy becomes a CANDIDATE.
- **The Judge** is a separate AI session (also Claude Opus 5.5). It keeps the SEALED-1 and FORWARD data, reviewed the evaluator for look-ahead before its first verdict, runs gate D, and reports only PASS, FAIL or INSUFFICIENT with aggregate numbers. It does not help authors tune.
- **The lead** writes the rules as numbered decisions. Thresholds change only through such a decision.
- **The owner**, kalambyrYura, performed every real-money action: starting and stopping Live rounds and installing releases.

## 3. The accounting model shared by all gates

- **Account.** One account of 100 USDT per strategy, at most one net position at a time, isolated margin, no leverage in the gate model. Leverage used in Live was a risk setting outside the gates.
- **Costs.** Every execution pays a 5 bps taker fee and 2 bps slippage. The model also has a spread of one tick, and funding.
- **Stress.** The same trades with an extra 5 bps on the notional of every execution, without re-simulating. The gates require a profit both at base costs and under stress.
- **Post-only (maker) orders.** A resting limit order fills only if the simulated price path after the order is placed goes through the limit by at least max(1 bps, 2 ticks) before the timeout. Touching the limit is not a fill. A maker fill costs a 2 bps fee plus 2 bps for adverse selection. An unfilled order is cancelled with no trade. Stop-losses are always taker orders. Reports also show a lower bound in which every maker fill is charged as a taker fill.
- **Funding.** Real OKX funding is charged wherever the data has it. Where a settlement is missing, a conservative scenario charges every missing 8-hour settlement against the position at max(average absolute funding rate over 2026-06-29 to 2026-10-02, 1 bps); for BTC this gives 1 bps. Gates are judged in this conservative scenario. For strategies that hold longer than 8 hours on average, reports also show the result at 3 bps per settlement (information, not a gate).
- **Periods.** A trade belongs to the period, quarter and year of its entry.
- **End of data.** A position still open when the data ends is closed at the last price with taker costs and flagged.
- **Errors.** A window that fails because of a data or engine error fails the gate until the cause is fixed. Windows are never skipped.

The official evaluator is `tools/team_eval.py`. For recipes in format version 4 (both final candidates use it) it runs the candle engine `tools/team_eval_v4.py` with execution model `v4-path5s-2`:

- each 1-minute bar becomes a 5-second price path, open-low-high-close or open-high-low-close, with a one-tick spread;
- features use closed bars only. A bar becomes visible 5 seconds after it closes; there is one decision per newly visible bar, and an order reaches the book at the next quote;
- if any feature an entry rule needs is unknown (for example after a data gap), the rule cannot enter;
- a stop is active from the fill, a take-profit only from the next minute; if both could trigger within the same minute, the stop wins. Exits fill at the next quote, at the price the position can be closed at, not at the stop level;
- funding is charged only for settlements actually recorded between fill and exit, valued at the previous mid price;
- order size is rounded down to the exchange lot; below the minimum size the bot holds.

Before the evaluator was used for any verdict it passed causality tests: future data was poisoned at 500 or more moments, including day, hour, funding, week and daylight-saving boundaries, plus truncation, warm-up, shift and availability tests and reference vectors. A session that had not written it reviewed it. For a taker recipe it reproduced the team's exact production engine trade for trade (610 of 610 trades over 90 seasons in 2025Q1, difference 0.0 bps). The Judge reviewed it again before the first gate D verdict and found no look-ahead; its findings were limits of the decision span and one known model limit (see Section 11).

## 4. Gate K: the historical test

Gate K decides whether a strategy becomes a CANDIDATE. It uses DEV data only, and every check must pass:

| # | Check | Threshold |
|---|---|---|
| K1 | Form | the recipe is in the competition format and passes the validator and the linter |
| K2 | Whole DEV | net > 0 and stress > 0 |
| K3 | Size and worst year | at least 100 closed trades; no calendar year below -10 USDT (-10% of capital) |
| K4 | Modern epoch E2 | net > 0 and stress > 0; at least 40 closed trades; at least 6 active quarters, with net > 0 in at least 60% of them |
| K5 | Tail of E2 | at least 15 closed trades and net > 0 at base costs (stress is reported) |
| K6 | Old epoch E1 | result under stress not below -5 USDT (-5% of capital) |
| K7 | Not a few trades | net without the 10 best trades >= 0 |
| K8 | Paper trail | a pre-registration (PREREG) made before the test, and a complete log of attempts (TRIALS) including failures, with the number of configurations tried |

- A quarter is active if it has at least 5 closed trades. Quarters with 0-4 trades are left out of the share, but their results count in the sums. The quarter checks test the stability of one fixed recipe; they are not a re-fit per quarter.
- An epoch the run covers by less than 95% is NOT_EVALUATED, and the verdict is then NOT_ALL_CHECKS_EVALUATED, not PASS.
- Gate K verdicts: PASS, FAIL, NOT_ALL_CHECKS_EVALUATED, or NEEDS_SEQUENTIAL_RUN (only for the older exact engine, when more than 5% of trades had to be dropped while joining day windows into one account).

**Worked example: H10-ETH, the only CANDIDATE.** Official evaluator, whole DEV of ETH-USDT-SWAP:

| Check | Value | Threshold | Result |
|---|---|---|---|
| net / stress, whole DEV | +30.81 / +25.33 USDT | > 0 / > 0 | PASS |
| closed trades | 550 | >= 100 | PASS |
| worst year | +0.53% of capital (2025) | >= -10% | PASS |
| without the 10 best trades | +19.81 USDT | >= 0 | PASS |
| E2 net / stress | +6.68 / +4.75 USDT | > 0 / > 0 | PASS |
| E2 trades / active quarters / share positive | 200 / 11 / 63.6% (7 of 11) | >= 40 / >= 6 / >= 60% | PASS |
| tail | 93 trades, net +6.03, stress +5.13 USDT | >= 15 trades, net > 0 | PASS |
| E1 under stress | +20.58 USDT | >= -5 USDT | PASS |

The margin was thin. Had one of the seven positive E2 quarters turned negative, E2 would have failed (6 of 11 is 54.5%). E2 net was about 2.4% a year on the 100 USDT account. This is why the team treated gate K as a necessary filter, not as evidence of an edge.

## 5. Track K-T: gate K for long holding periods

A strategy that holds positions for days or weeks makes only about 4-15 trades a year per instrument. It can never reach gate K's 100 trades, 40 in E2 or 15 in the tail, and "without the 10 best trades" works against the way trend strategies earn. Simply lowering the minimums would let market growth through:

- a synthetic strategy that buys on random days and holds for weeks has net > 0 and stress > 0 on DEV in 88-98% of cases;
- in one author's research, random long entries with the same holding times earned 462 bps per trade, against 622 bps for the real entries.

Track K-T therefore asks a different question: **are these entry and exit dates better than the same trades placed at other dates?** The test grew out of a random-entry control proposed by one of the authors (Sol).

Status at the archive: the Judge wrote the rule and the lead adopted it on 2026-11-17 for future long-horizon research. No strategy was ever submitted under it. An official K-T verdict needs a ledger from an official evaluator that can hold positions for weeks; that extension was planned but not built. On any other trade list the tool labels its result INDICATIVE.

### Who may use the track

- The author declares the track in the PREREG before any run.
- Fewer than 100 closed trades on DEV and a median holding time of at least 72 hours; otherwise standard gate K applies. H10, with 550 trades held about 24 hours, does not qualify.
- One configuration, chosen by a rule fixed in the PREREG on data up to 2025-06-30. The tail (2025-07-01 to 2026-10-02) is looked at once in the whole research cycle.
- The same account and costs as gate K.

### Checks (all must pass)

| # | What it checks | Threshold |
|---|---|---|
| T1 | volume | closed trades: DEV >= 15, E2 >= 6, tail >= 2 |
| T2 | result on DEV | net > 0 and stress > 0 |
| T3 | no collapses | every calendar year >= -10 USDT; E1 under stress >= -5 USDT |
| T4 | timing on DEV, corrected for choosing the best of m configurations | single form: p <= 0.05 / m; family form: family p <= 0.05 / q |
| T5 | modern epoch E2 | net > 0 and stress > 0; timing p <= 0.10; net without the best of six half-years >= 0 |
| T6 | tail | net > 0; timing p <= 0.50 |
| T7 | not a few trades | DEV without the 2 best trades >= 0; E2 without the best trade >= 0 |

Verdicts: PASS, FAIL, or NOT_ELIGIBLE (the strategy does not belong to this track).

Why these choices:

- T4 is the main statistical guard. Market growth passes the other checks easily.
- T5 makes sure the edge did not stay in the past: in E2 the real dates have to rank among the best 10% of shifts.
- T6 is mild because the tail has 458 days and only a few trades; a strict threshold there would reject real edges too. In exchange, the tail is clean: the selection never saw it.
- T7 replaces "without the 10 best trades": with 30-70 trades, the ten best are most of the profit of any trend strategy.
- Half-years replace quarters because with 4-15 trades a year many quarters hold one trade or none.

### The timing test

1. For each day of DEV, the exposure is the signed entry notional divided by 100 USDT if a position is open at noon UTC that day, and 0 otherwise.
2. The day's result per 100 USDT of notional is the close-to-close price change minus that day's funding (or the conservative charge where funding is unknown).
3. S(0) is the sum of exposure x day result over the real dates. S(k) is the same sum with the whole exposure pattern moved k days later, wrapping around inside the period, for every k from 1 to T - 1. Every shifted copy has the same trades: the same number, lengths, sides and pauses. Only the dates differ.
4. p = (1 + number of shifts with S(k) >= S(0)) / T.

What this gives:

- Buy-and-hold, or any random choice of dates, gets p near 0.5: market growth reaches every shift equally.
- Costs do not matter for the test, because every shift pays the same.
- Notional is held constant, with no compounding inside a trade, so one giant trend cannot decide the result.
- Every shift counts, the nearest ones too. Under random placement the chance of p <= a is then at most a, also for small thresholds.

### Correcting for choosing the best of m configurations

An author may try up to 24 configurations and submit the best. The best of 24 random rules looks good on its own, so T4 corrects for the choice. The author picks one of two forms in the PREREG before any run; the default is the single form.

- **Single form (Bonferroni).** p <= 0.05 / m, where m counts every configuration declared in the PREREG, all instruments together. With m = 24 the threshold is 0.0021. Fewer declared configurations give a milder threshold.
- **Family form.** The author hands in the trade lists of every configuration declared on this instrument, and the Judge checks them against the PREREG and TRIALS. All configurations are shifted together by the same k. For each one, z(k) is S(k) standardized over all shifts. The family p = (1 + number of shifts where the best z(k) in the family >= the z of the submitted configuration at the real dates) / T. The threshold is 0.05 / q, where q is the number of instruments the PREREG allowed choosing from (q = 1 if only one).

The family form is exact for any way of choosing the configuration, and it does not punish similar configurations: twenty neighbouring parameter values of one rule behave almost like one try, and the test sees that. The single form counts them as twenty independent tries.

Section 7.1 gives the calibration of this track.

### Gate D for long horizons: a draft only

Gate D needs at least 20 sample trades and two Live rounds of 24 hours. At 4-15 trades a year, 20 forward trades take 1.5 to 5 years, and a position held for weeks does not fit into a one-day round. The Judge drafted an adaptation (a declared sample of at least 365 days with at least 6 closed trades, the same timing test with p <= 0.10, Live seasons of 7-30 days), but it was never adopted. The Judge's own conclusion: for such strategies there is no honest forward proof faster than about a year.

## 6. Gate D: the forward test

Gate D decides whether a CANDIDATE is PROVED. Only the Judge runs it, on data the author never saw.

| # | Check | Threshold |
|---|---|---|
| D1 | Submission | a frozen recipe identified by its sha256; at most 2 submissions per author in 14 days |
| D2 | Result | official evaluator, same costs and stress as gate K: net > 0 and stress > 0 |
| D3 | Sample size | at least 20 closed trades. The author declares N >= 20 when submitting; the verdict is computed on the first N closed trades of the sample and is final for that sha256. Until then the verdict is INSUFFICIENT |
| D4 | Concentration and risk | largest trade <= 30% of gross profit; maximum account drawdown, open position included, <= 10 USDT |
| D5 | Live | at least 2 of the owner's Live rounds of 24 hours or more without execution anomalies, each with at least one fill of this recipe; actual slippage and fees no worse than the model by more than 3 bps |
| D6 | Reporting | only PASS / FAIL / INSUFFICIENT and aggregate numbers; authors never receive trade-level details of the sample |

### The sample

- SEALED-1 (2026-10-02 18:32 to 2026-10-28 00:00 UTC) and FORWARD (after 2026-10-28 00:00 UTC) form one continuous series of minutes. The Judge joined them into one store with no missing minute at the joins.
- The sample is evaluated as one 100 USDT account with one position. The account starts flat; only the feature warm-up is read from DEV.
- A trade belongs to the sample if its entry fill is not earlier than the sample start.
- The sample starts at the later of two moments: (a) the freeze, when the recipe's sha256 appears in the main branch (for a recipe frozen before SEALED-1, the start of SEALED-1); (b) the merge of the submission with its declared N. A submission merged later still counts from the freeze if no Live round ran or started between the two. Times come from the merge commits, not from the authors' own commit times.
- Recipes created after the authors had seen Live data (the November 2026 research cycle) are judged on FORWARD data from their freeze only. A version frozen after its author looked at Live packets is judged only on data after its freeze.

### Counting the first N trades

The official evaluator (option `--declared-n N`) and the Judge's independent script (`team/judge/tools/gate_d_first_n.py`) compute the same thing and must agree on a hash of the trade prefix:

1. Take the trade ledger of the official run on the gate D store.
2. Keep one account in order of entry time and exit time: a trade that enters before the previous kept trade exits is dropped.
3. Remove trades closed only by the end of the data or by a data gap (`forced_close_end_of_period`, `data_gap_exit`). They are not closed trades: they wait for more data and count later, with their real exit.
4. Sort the rest by entry time and take the first N.
5. Compute D2 and D4 on these N trades only. The drawdown is measured on the account with the open position, marked at every 5-second quote at the price the position could be closed at, exit fee included, up to the exit of the N-th trade. This is stricter than marking at each minute's close.
6. Status: fewer than N closed trades gives INSUFFICIENT. A data or engine error before the N-th exit gives NEEDS_DATA_FIX, which is not a verdict; the run is repaired. More than 5% overlapping trades gives NEEDS_SEQUENTIAL_RUN. Otherwise the result is FAIL if any D2 or D4 check fails, else PASS_PENDING_LIVE (D5 is checked outside the tool).
7. Record the sha256 of the ordered prefix (canonical JSON), of the ledger file and of the run's registry line, and the exact command.

Once the N-th trade has exited, neither later trades nor later data change the verdict.

### When the evaluator code changes

A submission fixes a tuple: recipe sha256, N, execution model id, and the hashes of the evaluator, the candle engine and the core code. If a new execution model appears before the verdict, the verdict at the N-th trade is computed under every model in force since the submission, and the worse one counts (INSUFFICIENT is not worse than PASS or FAIL). After a verdict, a model change can only make it worse: PASS can become FAIL, FAIL never becomes PASS. A code change that keeps the same execution model is accepted only if the same trades come out on DEV and in the sample. The Judge re-ran H10's sample after each such change and got the same trades every time.

### A second window for a recipe that failed gate K

SOL-NC1-K6-F0-EK failed gate K but was submitted. Before any evaluation, the lead wrote down what a result would mean:

- PASS on the first 30 trades: "forward PASS, not proved". The recipe stays in Live at one lot, with no larger size or leverage.
- PROVED only after a PASS in a second, non-overlapping window: trades 31-60, the same thresholds, with the drawdown measured from the account level before the window's first entry.
- FAIL in any window: the recipe leaves Live.
- INSUFFICIENT: the sample continues.
- D5 stays a separate requirement for PROVED.

The Judge's script computes the second window with `--window 2`.

### The Live part (D5)

A round counts for a candidate if:

- its admission window for that candidate lasted at least 24 hours; a paused or stopped round is not a round;
- the candidate had at least one fill of its own entry order;
- execution was clean: fills, stops and exits reconciled with exchange confirmations; the protective stop placed within 5 seconds (more than 10 seconds is an anomaly); recipe exits executed within 60 seconds of exchange time; no manual reconciliation for that bot; short outages within set limits;
- decisions were made on at least 95% of the bars of the window, with the first entry check no later than 60 minutes after the start.

Rounds with leverage count under the same rules plus isolated margin per bot, a liquidation distance of at least twice the recipe's stop, and no liquidation.

### Reporting

With each verdict the Judge reports closed and still-open trades, net, stress, account drawdown, the largest-trade share, the order funnel (signals, orders, fills, unfilled, rejected), the actual decision span, and the hashes of the recipe, evaluator, core and data catalogs. With a PASS or FAIL it also reports the **strength of evidence**: how often a null model passes. The null model is a bootstrap of N trades drawn from the author's DEV trades shifted to zero mean, that is, the same trade shape with no edge. This number does not change the status. It tells the reader how much a verdict on N trades can mean.

## 7. Calibration: how often the gates are fooled

### 7.1 Track K-T, on synthetic strategies

All of this calibration used synthetic strategies on top of real DEV daily prices of BTC, ETH and SOL: positions of 12-40 days, 3-11 trades a year, long-only and long/short. A simulated author declares up to 24 configurations and submits the best one under stress on the selection period. An edge is added by an "oracle with noise" that keeps trades with good outcomes more often. No real trading rule was run on DEV to set the thresholds. Scripts, seeds and outputs are in `team/judge/calibration/gate_k_long_2026-11-17/`.

**What the edge levels mean** (one configuration, no selection; the first number is for about 11 trades a year held 12 days, the second for 3-4 trades a year held 40 days):

| Level | Winning trades | Gain over the same trade at random dates | Timing z-score on DEV |
|---|---|---|---|
| none | 51-52% | about 0 | about 0 |
| weak | 60-61% | +300 to +600 bps | 2.0 to 1.1 |
| medium | 66-69% | +490 to +930 bps | 3.1 to 1.8 |
| strong | 78-80% | +770 to +1,400 bps | 4.6 to 2.6 |

The z-score says by how many standard deviations the real dates beat the average shift.

**Share passing all K-T checks, single form.** Mean over 18 cells (instrument x side x frequency), worst and best cell in brackets; 1,000 runs per cell without an edge, 400 with one.

| Edge | one configuration (m = 1) | best of 4 | best of 24 |
|---|---|---|---|
| none | 1.1% (0.2-2.0) | 1.5% (0.4-2.9) | 1.5% (0.4-3.2) |
| none, entries more often after volatile days | 1.1% (0.4-1.8) | 1.4% (0.2-2.5) | 1.4% (0.4-2.6) |
| weak | 19% (5-36) | 25% (5-55) | 27% (3-62) |
| medium | 45% (16-76) | 53% (13-84) | 51% (13-88) |
| strong | 81% (41-100) | 84% (45-100) | 78% (27-99) |

In the "best of m" columns every one of the m configurations has the edge, and the best one also gains from the selection. That is why the pass rate does not fall with m the way it would for one real configuration among random ones.

The spread in brackets is mostly trade frequency. One configuration:

| Trades a year | none | weak | medium | strong |
|---|---|---|---|---|
| about 11 | 1.2% | 29% | 65% | 98% |
| about 7 | 1.3% | 18% | 47% | 89% |
| about 3.5 | 0.8% | 8% | 22% | 57% |

T4 alone holds its nominal level: without an edge it passes 3.1-4.7% of the time at a nominal 5%.

**Family form against single form, best of 24** (12 cells; 600 runs per cell without an edge, 300 with one). "Independent" configurations are the worst case for selection; "similar" ones are variants of one trade list with an exposure correlation of about 0.7, like neighbouring parameters of one rule.

| Edge | independent, single | independent, family | similar, single | similar, family |
|---|---|---|---|---|
| none | 1.3% (0.0-2.3) | 1.6% (0.0-2.8) | 0.7% (0.2-1.3) | 1.3% (0.3-2.5) |
| weak | 27% | 30% | 11% | 15% |
| medium | 50% | 53% | 32% | 38% |
| strong | 74% | 79% | 61% | 66% |

With q = 3 (a choice among three instruments) the family form passes 0.5-0.7% without an edge and 60-73% with a strong one. A family of similar configurations passes less often than an independent one, because its variants are spoiled copies of one trade list: choosing among them adds almost nothing.

**What T5 and T6 add.** Tested on a synthetic strategy that has an edge only until the end of 2023 and trades at random afterwards. The range runs from one configuration with a medium edge to the best of 24 with a strong one; 12 cells of 300 runs.

| Checks applied | pass rate of the strategy whose edge vanished |
|---|---|
| T1-T4 and T7 only, nothing on E2 or the tail | 57-80% |
| plus net > 0 in E2 and in the tail, no timing there | 19-32% |
| plus timing: E2 p <= 0.25, tail p <= 0.50 | 15-29% |
| the adopted rule: E2 p <= 0.10, tail p <= 0.50 | 8-19% |

The stricter E2 threshold costs a strategy with a real edge 2-10 points of pass rate (medium edge, one configuration: 55% to 45%), while a strategy whose edge stayed in the past passes one and a half to two times less often. The tail is too short for a stricter threshold: 0.25 there would take another 6-8 points from a real edge.

**Trend rules on artificial prices.** A trend rule enters after a move and exits after a reversal, so its positions depend on past prices even when nothing is predictable. To check that the test does not reward this, trend rules ran on artificial prices with no predictable direction: real DEV daily changes in random order, and real daily volatility with a random sign. The rules were channel breakouts (20 and 55 days) and time-series momentum (20, 50 and 100 days), long-only and long/short, 1,200 paths each.

| Threshold p | nominal rate | rate on artificial prices |
|---|---|---|
| 0.25 | 25% | 19-26% |
| 0.05 | 5% | 0.4-3.6% |
| 0.0125 | 1.25% | 0-0.17% |
| 0.006 and 0.002 | 0.6% and 0.2% | 0 of 24,000 |

So the test does not give trend rules false passes above the nominal rate, and at small thresholds it is much stricter than nominal. As a control, when the artificial prices keep real 20-day blocks of DEV in random order, rules with a 20-day window reach p <= 0.05 in 4-20% of cases: the test reacts to real momentum within the blocks, as it should. These rules were never run on the real DEV price path.

**Why not a bootstrap of days in the market** (an alternative one author proposed)? It checks whether the average day in a position is positive, and market growth delivers that for long positions without any timing. On synthetic strategies with no timing at all (300 per cell, blocks of 20 days in the market, 2,000 resamples):

| What passes | long only | long/short |
|---|---|---|
| bootstrap on DEV >= 0.90 | 48% | 12% |
| bootstrap on DEV and in E2 | 18% | 5% |
| bootstrap plus net > 0 on DEV, in E2 and in the tail | 12% (9-14) | 4% (3-6) |
| all K-T checks, one configuration | 0.9% | 1.1% |

For long/short strategies the bootstrap holds its level, because the position sign removes market growth. For long-only strategies it lets one no-edge strategy in eight through.

### 7.2 Gate D at N = 30

The authors measured how often the numeric gate D tests (net > 0, stress > 0, largest trade <= 30% of gross profit, drawdown of closed trades <= 10 USDT) pass on their own DEV trades:

| Candidate | What was measured | Pass rate |
|---|---|---|
| H10-ETH | rolling windows of 30 trades: DEV / E2 / tail | 72% / 63% / 83% |
| H10-ETH | bootstrap of 30 trades from E2, as recorded | 64% |
| H10-ETH | the same bootstrap shifted to zero mean (no edge) | 43% |
| SOL-NC1-K6-F0-EK | windows of 30 consecutive trades: DEV / E2 / tail | 57.5% / 50.3% / 28.0% |

For H10, a no-edge version of the same trade distribution passes about 43% of the time, against 64% with the recorded edge. A PASS on 30 trades would therefore raise the odds of a real edge only about 1.5 times, and a FAIL would be weak evidence too. H10's average E2 trade made +0.033 USDT with a standard deviation of 0.344 USDT, about 0.1 standard deviations per trade. N = 30 was chosen because at 20 trades a real edge of E2 size would fail about half the time, mostly on the largest-trade check, while going from 30 to 40 trades added little (64% to 67%) for about 50 more days of waiting. For SOL-NC1-K6-F0-EK, a FAIL was likely even with a real edge of the size seen on DEV.

These windows use the drawdown of closed trades, a simpler measure than the official D4.

### 7.3 Standard gate K

The standard gate K was not calibrated on synthetic strategies the way track K-T was. Its protection against luck is the trade minimums, the modern-epoch and tail checks, the "without the 10 best trades" check, the controls each PREREG had to include (Section 8), the caps on configurations and, above all, gate D on unseen data. For long holding periods, the K-T work shows why the minimums matter: without them, random long strategies held for weeks pass the net and stress checks on DEV in 88-98% of cases.

## 8. Rules against overfitting

1. **Pre-registration in Git before any run.** Before each series of tests, the author writes a PREREG: the hypothesis, what exactly is tested, the parameters or grid, and the rule that will pick the winner. It is committed to Git together with its line in the TRIALS log, in its own commit, before the first run. The commit time is independent evidence. This was tightened after the first candidate: for H10, a pre-registration was committed 1.5 minutes after the first look at results, so "registered before the test" rested on log timestamps alone.
2. **What a PREREG for a new line of research must contain.** Why the mechanism should still work after 2024, and what would refute it. Selection on 2024-01 to 2025-06, with E1 as a contrast. Controls: long and short separately; random entries in the same market regime; a placebo with the feature shifted in time; the share of neighbouring configurations that also work; and the gross move per trade against the cost wall (14 bps per round trip, 24 bps under stress). A descriptive look at new data before the PREREG counts as a run.
3. **One look at the tail.** The configuration is chosen on data up to 2025-06-30 by the pre-registered rule. The tail (2025-07-01 to 2026-10-02) is opened once, after the choice. If the tail fails, the hypothesis is closed; it is not re-tuned on the tail. If the tail had already been seen, for example because a recipe was built from components whose tails had been inspected, the DEV result counts as contaminated, the recipe is labelled "not proved" in advance, and only forward data can serve as evidence.
4. **The TRIALS log.** Every author keeps `TRIALS.jsonl`, an append-only log with one line per pre-registration, screen, run, check or verdict, each with a time stamp and the sha256 of the files involved. Failed and interrupted attempts stay in it. On every run the evaluator records the sha256 of the PREREG and TRIALS files and appends a line to a run registry, so no attempt disappears.
5. **Caps on configurations.** Up to 24 configurations per author per research cycle in the last two cycles (raised once to 30 for one author, recorded in advance, because the recipe format's limits had cut the author's idea short). Earlier limits were set per line of research, typically 20 configurations (12 for hybrids). When different authors' ideas overlapped, the lead counted them as one hypothesis with several tests.
6. **Multiple-testing correction.** Track K-T corrects explicitly: Bonferroni 0.05/m over all declared configurations, or the exact family test (Section 5). Standard gate K has no formal correction; it relies on the caps, the one look at the tail and gate D.
7. **The clone rule.** A new candidate is a clone of another participant's existing candidate if more than 50% of its entries fall within ±30 minutes of the other's entries, or if the correlation of daily PnL is above 0.8. The overlap is counted both ways and the larger share is used, so a rarer strategy hidden inside a more frequent one is also a clone. The check runs over the whole common period, separately in E2, and again on the gate D samples once both hold at least 20 trades. A clone's gate D verdict does not count until the lead decides, and only one strategy of a clone group is admitted. Example: SOL-NC1-K6-F0-EK against H10 had 8.0% entry overlap and a daily PnL correlation of 0.11 (8.5% and 0.14 in E2), so it was distinct. Against its own components it was a clone (80.3% and 97.8% overlap), so the components were not submitted.
8. **Hybrids.** Building on another author's CANDIDATE is allowed only with explicit attribution (`derived_from`), a PREREG in Git before the run, at most 12 configurations, one decisive look, and some information or mechanism of one's own. Re-tuning another author's candidate, moving it to another instrument, or adding a veto on top of it does not count as a hybrid.
9. **Independence.** Authors do not open each other's drafts or branches until a recipe becomes a CANDIDATE; they share only verified findings. Each research direction is tagged by family x information source x instrument x horizon, with at most two authors per cell.
10. **Walls around held-out data.** Authors never see SEALED-1 or FORWARD data except through sanitized Live packets. The Judge reports only aggregates, and reviews of Live rounds may not mention market facts from outside the round.
11. **Frozen recipes, fixed thresholds.** A submission is a frozen sha256 plus a declared N, and its verdict is final for that hash. A changed recipe is a new submission, judged only on data after its own freeze, and counts against the limit of 2 submissions per author in 14 days. Thresholds were fixed before the first submission and never relaxed.

## 9. Verdicts when the project was archived

On 2026-11-17 the owner decided to wind down trading and publish the project. The monthly gate D re-evaluations were cancelled with it, including the first one planned for 2026-12-01. The verdicts below are therefore the last ones.

The two recipes in one line each:

- **H10-ETH** (ETH-USDT-SWAP, long only): after a 60-minute rise of at least three typical hourly moves, with the price above its 20-day average, it places a post-only limit a quarter of a typical hourly move below the signal close, valid for 2 hours; it exits after 24 hours, at a 500 bps stop or at a 1,000 bps target.
- **SOL-NC1-K6-F0-EK** (SOL-USDT-SWAP, long only, taker entries): it enters when either branch fires. Branch L: the price is above its 20-day average, 5-30 bps under the 7-day high, and up at least 30 bps in the last hour. Branch K: the price is at least six typical hourly moves (30-day measure) below the 4-hour high and has risen over the last 15 minutes. Stop 500 bps, target 400 bps, maximum hold 12 hours.

| | H10-ETH | SOL-NC1-K6-F0-EK |
|---|---|---|
| Author | Sonnet (AI author persona) | MYHuman slot: the owner's slot; recipe assembled by an AI assistant session from components by Sol (branch L) and Opus (branch K) |
| Recipe sha256 | `dcf486662716f1b7a986c0e9f2f0d50d93d6fba8ad928d81f73541e77f906a81` | `e1f55979c09515109ab233f90fcb0ffe1fe955c0cf0cd44cb37e80fa01373114` |
| Gate K on DEV | PASS: 550 trades, net +30.81, stress +25.33 USDT; E2 positive in 7 of 11 quarters | FAIL on one check: E2 positive in 6 of 11 quarters (55%, needs 60%). 874 trades, net +66.99, stress +41.15 USDT; tail 161 trades, net +2.26, stress -2.47 USDT |
| Status | CANDIDATE, traded as TEST-LIVE | not a CANDIDATE; submitted to gate D labelled "not proved" |
| Declared N | 30 | 30 |
| Sample start | 2026-10-02 18:32 UTC (frozen before SEALED-1) | 2026-11-17 15:14:01 UTC (its freeze; FORWARD only) |
| Gate D verdict | INSUFFICIENT: 1 closed trade of 30, on SEALED-1 and FORWARD up to 2026-10-30 08:00 UTC | INSUFFICIENT: 0 of 30. Not evaluated: the Judge's FORWARD data ended before the sample started |
| Sample aggregates (informative only before N) | net -0.081, stress -0.092 USDT; account drawdown 0.24% of capital; 1 signal, 1 order, 1 fill | none |
| Live part (D5) | 7 Live rounds recorded (4 complete, 3 flagged incomplete); 0 signals and 0 fills in all of them; 0 counted | no Live round with this recipe |
| Strength of evidence at N = 30 | a no-edge version passes the numeric tests about 43% of the time | 30-trade windows on DEV pass 57.5% (DEV), 50.3% (E2), 28.0% (tail) |
| Clone check | not needed (first candidate) | distinct from H10 |

Notes:

- H10 traded far less in the sample than on DEV: one order and one trade in 27.5 days, against about 0.2 trades a day in the DEV tail. One short stretch says little; it only means the 30th trade lay months ahead.
- After H10's submission the core and evaluator code changed several times without changing the execution model. The Judge re-ran the sample after each change and got the same trades.
- A last Live round started on 2026-11-17 and was still running when this document was written; it is not included.
- The two research cycles of November 2026 found no new candidate. In the first (holding times of hours to days), no author had a rule that passed gate K. In the second (days to weeks), the DEV results were mostly market growth: all 24 configurations of one author's channel-breakout family were positive on DEV, and the best made +95.5 USDT on 55 trades, but -0.79 USDT on 11 trades in the tail (indicative results from the author's own engine, not the official evaluator).

No strategy reached PROVED. This project does not claim that any of its strategies has an edge.

## 10. Running the gate tools yourself

Everything here uses public OKX market data; no exchange account or keys are needed. The team's commands use the Windows launcher `py -3.12 -X utf8 -B`; elsewhere, call Python 3.12 with the same flags (`-X utf8` forces UTF-8 text, `-B` skips writing .pyc files). The tools need numpy. Installation and repository setup are covered by the quickstart guide. The commands below follow each tool's documented usage; they were not re-run for this document.

### 10.1 Data

1. Download 1-minute candles and funding (GET requests to OKX public endpoints only):

```
py -3.12 -X utf8 -B tools/public_history.py fetch --inst ETH-USDT-SWAP --start 2020-01-01 --cutoff 2026-10-02T18:32:35.865Z --out <folder>
py -3.12 -X utf8 -B tools/public_history.py verify --out <folder>
```

The cutoff above is the DEV cutoff: only candles closed by then are kept, so the last one opens at 18:31 UTC. Index candles come from `--kind index` (for example `--inst ETH-USDT --rate 4`), mark-price candles from `--kind mark`.

2. Build and check the store catalog that the evaluator reads:

```
py -3.12 -X utf8 -B tools/team_data.py catalog --root <store> --inst ETH-USDT-SWAP
py -3.12 -X utf8 -B tools/team_data.py verify --root <store>
```

Index and mark stores sit next to the swap store, named `<BASE>-USDT_index` and `<instrument>_mark`.

- **Funding.** OKX's public funding endpoint only reaches about three months back. The team added OKX's official monthly funding archive (from 2021-09). Without it, the evaluator charges every missing settlement under the conservative scenario, so your numbers will differ from the team's.
- **Size.** A full DEV of ETH is about 750 MB, of SOL about 577 MB.
- **A known gap.** OKX does not return one SOL minute (2026-08-28 08:30 UTC). The candle engine fills a single missing minute with a flat candle and makes no decision on it.

### 10.2 Gate K

```
py -3.12 -X utf8 -B tools/team_eval.py run --recipe <recipe.json> --set dev --store <DEV store> --author <name> --label <label> --prereg <PREREG file> --trials <TRIALS.jsonl> --out <out>
py -3.12 -X utf8 -B tools/team_eval.py gates --ledger <out>/ledger.json
py -3.12 -X utf8 -B tools/team_eval.py clone --a <new>/ledger.json --b <existing>/ledger.json [--epoch E2]
```

- `--out` receives `ledger.json` (every trade with every execution), `gates.json` (metrics and verdict) and `gates.md` (a readable table). Each run also appends a line to a run registry (`--registry`; by default under `runtime/team_eval/<author>/`).
- In DEV mode the evaluator never reads data after 2026-10-02 18:31 UTC.
- `gates` recomputes the gates from a saved ledger without re-running the engine.
- A format-4 recipe over the full DEV of ETH took 100-170 seconds in one process, with peak memory of about 1.4 GiB committed; the tool refuses to start with less than 3 GB of free RAM. Older V1/V2 recipes run on the exact engine, which took 30-60 minutes for the full DEV, depending on the number of worker processes.

### 10.3 Track K-T

```
py -3.12 -X utf8 -B team/judge/tools/gate_k_long.py --ledger <DEV ledger.json> --configs <m> --store <DEV store> --out <dir>
py -3.12 -X utf8 -B team/judge/tools/gate_k_long.py --trades <trades.json or .csv> --instrument SOL-USDT-SWAP --configs <m> --store <DEV store> --out <dir>
```

- `--configs` is m, the number of configurations the PREREG declared (default 24). For the family form add `--family <files, or a folder with the trade lists of every configuration declared on this instrument> --instruments <q>`.
- A trade list needs `entry_at` and `exit_at` (ISO, UTC), `side` (`long` or `short`), `notional_usdt` (entry notional on the 100 USDT account), `net_usdt` (after all costs and funding), and optionally `executed_notional_usdt` (default: twice the entry notional). Results from a trade list are labelled INDICATIVE.
- Output: `gate_k_long.json` and `gate_k_long.md`, with every check, timing p-values by period, and the sha256 of the inputs and of the tool.

### 10.4 Gate D on data after the DEV cutoff

The SEALED-1 and FORWARD periods are now ordinary public market data, so you can run the same count on the two frozen recipes. The result is your own, not a verdict of this project, and the Live part (D5) cannot be reproduced without real rounds.

1. Build a store of 1-minute candles and funding that starts at 2026-10-02 18:32 UTC, with no missing minutes.
2. The forward mode reads its feature warm-up from the DEV store of the same instrument. It looks for a folder named after the instrument inside `data/okx/` (the default of `DEFAULT_STORE` in `tools/team_eval.py`), so keep your DEV stores there, for example `data/okx/ETH-USDT-SWAP`. No code change is needed.
3. Check the recipe. The evaluator writes `recipe_sha256` into the ledger: H10-ETH must give `dcf486662716f1b7a986c0e9f2f0d50d93d6fba8ad928d81f73541e77f906a81`, SOL-NC1-K6-F0-EK `e1f55979c09515109ab233f90fcb0ffe1fe955c0cf0cd44cb37e80fa01373114`. Any change to the recipe's content, including its descriptive fields, changes the hash.
4. Run the evaluator with the declared N. The tool cannot start earlier than 31 minutes after the first minute of the store, and without `--start` it begins at the next 00:00 UTC. The Judge ran H10 like this and checked the first 31 minutes of SEALED-1 separately (no signal there):

```
py -3.12 -X utf8 -B tools/team_eval.py run --recipe <H10 recipe.json> --set forward --store <post-DEV ETH store> --start 2026-10-02T19:03:00Z --declared-n 30 --out <dir>
```

For SOL-NC1-K6-F0-EK, whose sample starts at 2026-11-17 15:14:01 UTC, the procedure only fixes that the account is flat at the sample start and that a trade counts if its entry fill is not earlier. Start the run there with `--start`. The Judge never ran this sample, so this step was never tested.

5. Cross-check with the Judge's independent script; both must report the same prefix hash and verdict:

```
py -3.12 -X utf8 -B team/judge/tools/gate_d_first_n.py --ledger <dir>/ledger.json --n 30 --command "<the exact run command>" --out <file.json>
```

Add `--window 2` for the second window (trades 31-60).

The evaluator also stops at the last whole 24-hour window of a store; the Judge covered the latest hours at the next data load.

### 10.5 Reproducing the K-T calibration

The scripts in `team/judge/calibration/gate_k_long_2026-11-17/` regenerate every number in Section 7.1. Seeds are fixed in the scripts, so the numbers reproduce exactly; the whole set took about 40 minutes on one core, with under 100 MB per process. Copy the scripts into an empty work folder `<work>`; `<repo>` is the repository root. `dev_daily.py` reads the DEV stores from a folder set inside the script, so point that at your own stores first.

```
py -3.12 -X utf8 -B dev_daily.py <repo> <work>/dev_daily.json
py -3.12 -X utf8 -B kt_calibrate4.py <work> 1000 400 <work>/kt_raw4.npz
py -3.12 -X utf8 -B kt_calibrate5.py <work> 600 300 <work>/kt_raw5.npz
py -3.12 -X utf8 -B kt_analyze.py <work>
py -3.12 -X utf8 -B kt_skill_scale.py <work> 150
py -3.12 -X utf8 -B kt_pathdep.py <work> 400
py -3.12 -X utf8 -B kt_pathdep2.py <work> 400
py -3.12 -X utf8 -B kt_decay.py <work> 300
py -3.12 -X utf8 -B kt_decay2.py <work> 300
py -3.12 -X utf8 -B kt_bootstrap_null.py <work> 300
py -3.12 -X utf8 -B kt_tool_check.py <work> <repo>
py -3.12 -X utf8 -B kt_tool_check_family.py <work> <repo>
```

The printed outputs (`*.out`) are kept next to the scripts. The last two scripts compare `gate_k_long.py` with the calibration functions on synthetic trade lists.

### 10.6 Tests

The evaluator's own tests are in `tests/test_team_eval.py` (among them a conformance test against the exact engine and a comparison of `--declared-n` with the Judge's independent script `team/judge/tools/gate_d_first_n.py` on 80 random ledgers) and in `tests/test_team_eval_v4.py`.

## 11. Known limits

- **Execution is a model.** Each minute is a 5-second path, the spread is one tick, and visible volume is fixed. Post-only fills depend on this path model, not on a real order queue, and the direction of the error is unknown. Reports show an "all maker as taker" lower bound and a cost grid; Live calibration (D5) is required before PROVED.
- **Funding history is incomplete.** There is no real funding history before 2021-08-31, and there is a gap from 2021-09-30 to 2022-01-16. The conservative charge stands in for it.
- **AI authors may know the past.** The models behind the authors were trained on data that may include market history up to mid-2026, so even the DEV tail was not fully blind for them. Only data after the DEV cutoff was.
- **Gate K is a filter, not proof.** On synthetic data, 1-2% of no-edge long-horizon strategies pass track K-T, and 8-19% of strategies whose edge vanished after 2023. For standard gate K no such rate was measured.
- **Gate D at N = 30 is weak.** For H10, a no-edge version passes about 43% of the time.
- **Weak edges are hard to prove.** With 6.75 years of history and a few trades a year (25-70 trades in total), a weak edge cannot be told apart from luck. This is a limit of the data, not of the rule; loosening the thresholds would let market growth through.
- **K-T's power on real trend rules is unknown.** The calibration used a synthetic edge that does not depend on entry mechanics. Late entries and exits of real trend rules may give worse p-values for the same benefit. The single form is nearly closed to trend rules for m >= 4: on artificial prices they reached p <= 0.0125 in only 0-0.17% of cases, so the family form on one instrument, or one or two configurations, is the realistic route.
- **Adding to positions in K-T.** Daily exposure uses the first entry's notional, an approximation for recipes that add to or partly close positions.
- **Repeated cycles.** The correction counts configurations within one PREREG. If the same idea runs through several cycles, the tries add up; that depends on the discipline of PREREG and TRIALS.
- **Small account.** 100 USDT per bot with a minimum BTC lot of about $8.5 means one size step is about 8.5% of capital, and absolute PnL is small.
- **One exchange, three instruments.** All results are for OKX perpetual swaps on BTC, ETH and SOL.

## 12. Glossary

- **bps**: basis point, 0.01%.
- **notional**: the value of a position in USDT.
- **net**: result after fees, slippage, spread and funding.
- **stress**: net minus an extra 5 bps of the notional of every execution.
- **taker / maker**: an order that executes against resting orders / a resting limit order. **Post-only**: a limit order that is rejected if it would execute immediately.
- **round trip**: an entry plus an exit.
- **funding**: periodic payments between longs and shorts of a perpetual swap, every 8 hours on OKX.
- **perpetual swap**: a futures contract with no expiry.
- **PREREG**: a pre-registered test plan, committed before any run.
- **TRIALS**: an author's append-only log of every attempt.
- **ledger**: the evaluator's file with every trade and execution of a run.
- **sha256**: a fingerprint of a file or a recipe; a frozen recipe is identified by it.
- **DEV, SEALED-1, FORWARD, E1, E2, tail**: see Section 2.
