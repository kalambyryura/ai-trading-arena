# Continue the ai-trading-arena research

The ai-trading-arena repository (github.com/kalambyrYura/ai-trading-arena) is the public record of the project this skill comes from: from 29 Sep to 17 Nov 2026, AI-designed bots traded OKX USDT-margined perpetual swaps on BTC, ETH and SOL, first with an OpenAI Codex agent and from 28 Oct with a team of Claude Code sessions. Paths below are relative to the root of that repository. It does not include the team's working files (channels, reviews, briefs, other author folders); this skill's templates are their generalized form.

## Contents

1. The honest state
2. Cheapest next step: extend the two forward tests
3. Ideas that were never tested
4. Where things are
5. Running a new team on this repository
6. What a new team must not do

## 1. The honest state

- **No edge was found.** More than 770 strategy configurations were tested with pre-registration and pass gates. No strategy reached PROVEN, and the project does not claim that any of its strategies has an edge.
- **Real money:** 86 trades lost 1.65 USDT net. Price moves earned +0.07 USDT, fees took 1.71 USDT, funding 0.01 USDT. The average trade gained about 0.4 bps before costs and paid 10 bps in fees.
- **H10-ETH** (author persona Sonnet; a post-only long entry on ETH after an upward burst in an uptrend, held up to 24 hours) is the only strategy that passed gate K, with a thin margin: 7 of 11 positive quarters in the modern epoch, where one more negative quarter would have failed it, and about 2.4 % a year on the 100 USDT account. Its gate D verdict is INSUFFICIENT: 1 of the 30 declared trades, on sealed and forward data up to 30 Oct 2026. It had no fill in the seven 24-hour live rounds it took part in, so no round counted toward gate D. Frozen `recipe_sha256` (the evaluator's hash of the recipe content, not the sha256 of the file) `dcf486662716f1b7a986c0e9f2f0d50d93d6fba8ad928d81f73541e77f906a81`.
- **SOL-NC1-K6-F0-EK** (the owner's MYHuman slot; a synthesis of two SOL long entries found by the authors Sol and Opus) failed gate K on one check (6 of 11 positive modern quarters, 60 % needed) and was submitted to gate D labelled "not proven" in advance. Its forward test started on 17 Nov 2026 at 15:14:01 UTC with 0 of 30 trades; it never traded live. The lead fixed in advance that a pass on the first 30 trades would mean only "forward pass, not proven" and that PROVEN would need a second window of 30. Frozen `recipe_sha256` `e1f55979c09515109ab233f90fcb0ffe1fe955c0cf0cd44cb37e80fa01373114`.
- **The two November research cycles found nothing.** NC1 (hours to days): no author had a rule that passed gate K. NC2 (days to weeks): development profits were mostly market growth; random entries with the same holding times earned 462 bps per trade against 622 for a channel-breakout rule, and the best breakout configuration made +95.5 USDT on 55 trades on development data but −0.79 USDT on its 11 tail trades (indicative numbers from the author's own engine).
- **Fast strategies lose by construction** at a regular fee tier: they caught 0 to 5 bps of gross movement per trade against 10 bps of taker fees per round trip. 12 of 16 early rule sets lost money even before fees.
- **The paid LLM filter** (a third-party decision model) cost $0.19 in total and added nothing measurable: on numeric inputs its answers were nearly constant and its confidence followed the number of conditions in the prompt.
- **On 17 Nov 2026 the owner wound trading down** and decided to publish the project as open source. The monthly gate D re-evaluations, the second window for SOL-NC1-K6-F0-EK and the planned long-horizon evaluator were cancelled.

Read `README.md`, `docs/RESULTS.md` and `docs/METHODOLOGY.md` before planning anything: they hold every number above with its source.

## 2. Cheapest next step: extend the two forward tests

Both frozen recipes can be judged on public data after their sample starts, without any account or key. This is the only evidence that cannot have been overfitted by the project.

1. Build a store of OKX 1-minute candles and funding from 2026-10-02 18:32 UTC (H10-ETH, ETH-USDT-SWAP) or 2026-11-17 15:14:01 UTC (SOL-NC1-K6-F0-EK, SOL-USDT-SWAP) with `tools/public_history.py` and `tools/team_data.py`.
2. Check that the evaluator reproduces the recipe hash, then run it in forward mode with the declared N, and cross-check with the judge's independent first-N script; both must report the same prefix hash:

   ```bash
   py -3.12 -X utf8 -B tools/team_eval.py run --recipe strategies/sonnet-h10-eth/recipe.json --set forward --store <post-cutoff ETH store> --start 2026-10-02T19:03:00Z --declared-n 30 --out <dir>
   py -3.12 -X utf8 -B team/judge/tools/gate_d_first_n.py --ledger <dir>/ledger.json --n 30 --command "<the exact run command>" --out <file.json>
   ```

   The forward mode reads its feature warm-up from a development store; `docs/METHODOLOGY.md` section 10.4 explains how to point it there and how the judge handled the first 31 minutes.
3. Report the result as yours, not as a verdict of the project, and state the strength of evidence: for H10 a no-edge version of the same trade distribution passes the numeric gate D checks about 43 % of the time at N = 30. The live part (D5) cannot be reproduced without real rounds.

At its development-tail rate of about 0.2 trades a day, H10 needs about five months for 30 trades; SOL-NC1-K6-F0-EK, at its tail rate of about 0.35 a day (0.43 on all of DEV), about 85 days.

## 3. Ideas that were never tested

Each needs a PREREG, a configuration budget and the gates, like everything else. None of them is a recommendation to trade.

1. **Maker execution at lower fee tiers.** The cost wall was a regular-tier taker round trip of about 14 bps with slippage, plus the spread. Rebate or market-maker tiers change the arithmetic, but a small account does not get them, and the team's maker lesson cuts the other way: post-only entries on continuation signals selected the worst fills (unfilled orders were the best trades). Test maker orders where the fill itself is part of the signal (a return to a level) or for exits, model the fee tier as a new execution model (which restarts gate D for any recipe it touches), and measure real fill rates and the move after each fill at minimum size before trusting any simulation.
2. **Other venues and instruments.** Everything covered OKX USDT perps on BTC, ETH and SOL, 2020–2026, one-minute candles. Other exchanges, other perps, spot, futures basis, options and cross-venue funding differences were never tested. The downloader and the executor are OKX-specific, so a new venue starts with a platform release (data adapter, cost and execution model calibrated on that venue). Transfer tests of unchanged frozen rules to another instrument, pre-registered, are the cheap version.
3. **Volatility-scaled sizing.** The recipe format had no way to size positions by recent volatility. It does not create a directional edge, but it changes drawdown and the largest-trade share, which gate D checks. It needs a schema and evaluator release with golden tests showing that existing recipes behave byte for byte the same.
4. **The long-horizon evaluator extension.** Official long-horizon verdicts (track K-T: a date-shift timing test corrected for picking the best of m configurations) need an evaluator that holds positions for up to 30 days with trailing stops and condition exits. It was planned and never coded. The judge's tool `team/judge/tools/gate_k_long.py` exists and labels results from other engines INDICATIVE. Expect weak power: on synthetic data a weak edge passes in 8–29 % of cases, and the single-form correction is nearly closed to trend rules for m ≥ 4, so use the family form or one or two configurations. An honest forward proof for weekly holds takes about a year.
5. **Order-book or on-chain data.** The research used only candles, funding, and index and mark prices. Order-book depth and imbalance, trades, liquidations, open interest and on-chain flows were never used. Public history for such data is short, so tests would be mostly forward-only and slow; start by recording it continuously and fix the evaluation plan before the data accumulates.
6. **LLM reading of news, over a long enough sample.** The J2 protocol (hourly headlines, typed questions, two code baselines, a placebo from the previous day, decision rules fixed before any call) collected only 31 of the 500 labelled hours it needed, lost 16 of 51 hours to recorder gaps, and its veto said "wait" in 84 % of hours. It gives no evidence either way. Rerunning it needs an uninterrupted recorder for about six weeks (roughly 1,000 calls, $0.05–0.10 at the measured prices), the model snapshot recorded with every answer, and a confirmed training cutoff and no web access during calls. See `strategies/opus-jev-j0-j2/`, `tools/news_recorder.py` and `team/judge/tools/j2_outcomes.py`.

Smaller follow-ups are listed at the end of each strategy card in `strategies/`, for example running the H10 rule on ETH and SOL together to collect trades faster, or pre-registering a calm-decline filter for SOL-NC1-K6-F0-EK and testing it only on data after 2026-10-02.

## 4. Where things are

| What | Where |
|---|---|
| Overview, numbers, FAQ | `README.md`; short machine-readable summary `llms.txt`; agent instructions `AGENTS.md` |
| Full results | `docs/RESULTS.md` |
| Gates, data periods, rules against overfitting, gate tool commands | `docs/METHODOLOGY.md` |
| Install, offline demo, data download, first backtest | `docs/QUICKSTART.md` |
| Code structure; recipe format | `docs/ARCHITECTURE.md`; `docs/RECIPE_SPEC.md` |
| How the AI team worked | `docs/AI_TEAM.md` |
| Strategy cards (16), with frozen recipes where publishable | `strategies/<slug>/` |
| H10-ETH frozen recipe | `strategies/sonnet-h10-eth/recipe.json`, identical to `team/authors/sonnet/candidates/H10_ETH/recipe.json` |
| Official evaluator (`run`, `gates`, `clone`; `--set dev|sealed|forward`, `--declared-n`) | `tools/team_eval.py`; candle engine `tools/team_eval_v4.py` |
| Public data downloader; store catalog | `tools/public_history.py`; `tools/team_data.py` |
| Judge's tools: first-N gate D count, long-horizon track, live round facts, news outcomes | `team/judge/tools/` (`gate_d_first_n.py`, `gate_k_long.py`, `live_round_facts.py`, `j2_outcomes.py`) |
| Recipe workshop (validate, lint, explain, test) | `tools/strategy_workshop.py` |
| Paid research harness with a per-role ledger, dry-run and replay | `tools/jev_research.py` |
| Heavy-run lease | `tools/research_limits.py` |
| Arena viewer on a synthetic market with a fake exchange | `tools/preview_arena.py --coliseum-proof 6` |
| Engine: strategy contract, simulation, OKX execution, reconciliation, arena | `trading_lab/` |
| Web UI (Ukrainian) | `web/`, served by `web_panel.py` |
| Offline tests with fake exchanges | `tests/` |
| Demo and probe recipes used by the panel and the tests | `03_For_Client_Deliverables/*.json` |
| Windows server scripts and root launchers | `deployment/server/`, root `*.cmd` |
| This skill | `skills/ai-research-team/` |

**Owner-only entrypoints in this repository** (the default guard config blocks all of them for agents): `one_trade.py --live` and `--web-run`; `--paid-once`; the panel's `/api/play`, `/api/stop`, `/api/exchange/start|stop|resume` and `/api/arena/start|stop|resume` routes; the root launchers (`START_*`, `RESTART_*`, `INSTALL_*`, `IMPORT_*`, `EXPORT_*`, `REISSUE_*`, `CHECK_*`, `NOTIFIER_*`); `tools/restart_panel.ps1` and the install, export and reissue scripts in `deployment/server/`; private order, transfer and account-setting endpoints. Keys are read by code only, from ignored `.env*` files and `LocalSettings/`, which the guard treats as secrets.

## 5. Running a new team on this repository

1. Clone the repository, install `requirements.txt`, and run the offline demo and the tests as `docs/QUICKSTART.md` describes. Everything runs without keys.
2. Scaffold a small team:

   ```bash
   python skills/ai-research-team/scripts/scaffold_team.py --repo . --roles lead,coord,platform,author,judge --authors alpha
   ```

   The scaffold never overwrites existing files (`team/judge/` and `team/authors/sonnet/` already exist). Then set `paths.vault` and `paths.dataStore` in `team/tools/guard.config.json`, and run `node team/tools/guard.cjs --check-config` and `--self-test`.
3. **Redraw the data partitions.** The old development set (to 2026-10-02 18:31 UTC) and its tail were looked at many times, and the models behind your authors may have been trained on that history. Treat everything up to your team's start as seen development data, fix a new forward period from your start date, and let only your judge touch it.
4. **Keep what held up:** the cost model (5 bps taker fee plus 2 bps slippage per fill, one-tick spread, real funding with a conservative charge where it is missing, +5 bps stress), the gate thresholds (or recalibrate them on synthetic data before the first submission and then fix them), the clone rule, pre-registration with TRIALS, one look at a tail, and the declared-N rule.
5. **Give the platform the first job:** rebuild the data store with manifests and decide whether the evaluator needs a release (for example the long-horizon extension or volatility sizing) before any author runs anything.
6. **Write down the stop rule with the owner** before the first cycle: what result, by what date, ends the research. The reference project needed seven weeks to learn that the answer was no.

## 6. What a new team must not do

- Present the old development results, a single live round or a demo as evidence of an edge.
- Re-tune the frozen recipes and keep their names or hashes: a changed recipe is a new submission, judged only on data after its own freeze.
- Run any live entrypoint, press any panel button that starts, stops or resumes a run, or touch keys: those stay with the human owner.
- Give financial advice. This repository is a documented negative result, not a trading system to copy.
