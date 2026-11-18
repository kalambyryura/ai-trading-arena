# Quickstart

This guide takes you from a fresh clone to three things, in this order:

1. a five-minute look at the tools, with no market data, no keys and no money;
2. a rerun of the official backtest of H10-ETH, the only strategy that passed the backtest gate, on free public data;
3. testing your own strategy idea against the same gates.

Sections 1 to 5 need no exchange account, no API keys and no paid services. Sections 3 and 4 download public market data from OKX; everything else runs offline. Placing real orders is covered last, in section 6, because you do not need it to check our result.

> [!NOTE]
> The web interface and some tool messages are in Ukrainian. Commands, file formats and code are in English.

## Contents

1. [Install](#1-install)
2. [A five-minute tour without data](#2-a-five-minute-tour-without-data)
3. [Reproduce H10-ETH](#3-reproduce-h10-eth)
4. [Test your own idea](#4-test-your-own-idea)
5. [Run the tests](#5-run-the-tests)
6. [Demo and Live: real orders](#6-demo-and-live-real-orders)

## 1. Install

You need:

- **Python 3.12.** The team used 3.12.10. The Windows server check insists on 3.12; other versions were not tested.
- **numpy**, required by the official evaluator for format-4 recipes, the Judge's long-horizon gate tool, the screening backtest and 25 of the 118 test modules, which do not even load without it. The rest of the code uses only the Python standard library.
- **Pillow**, optional. Only the local static-site export uses it, to convert the large arena images to WebP. Without it the PNG files are copied as they are and one test is skipped.
- **Node.js 18 or newer**, only for running the tests: the UI tests and a few Python tests use it. No npm packages are needed. The team used Node 24.

The team worked on Windows. The code has branches for macOS and Linux (file locks, memory checks), but nobody ran it there. If something breaks, please open an issue.

**Windows** (PowerShell or Command Prompt):

```
git clone https://github.com/kalambyrYura/ai-trading-arena.git
cd ai-trading-arena
py -3.12 -m pip install -r requirements.txt
```

**macOS or Linux:**

```
git clone https://github.com/kalambyrYura/ai-trading-arena.git
cd ai-trading-arena
python3.12 -m pip install -r requirements.txt
```

If your system Python refuses to install packages, use a virtual environment: `python3.12 -m venv .venv`, then `source .venv/bin/activate`, then `python -m pip install -r requirements.txt`, and type `python` instead of `python3.12` in the commands below.

How to read the commands in this guide:

- They are written for Windows: `py -3.12 -X utf8 -B`. On macOS or Linux type `python3.12 -X utf8 -B` instead. `-X utf8` makes Python read and print UTF-8 (many outputs contain Ukrainian text); `-B` stops it from writing `.pyc` files.
- Run every command from the repository root. Relative paths such as `data/okx/ETH-USDT-SWAP` are resolved from the current folder.
- Results go to `runtime/` and market data to `data/`. Both are local working folders: do not commit them, and delete them whenever you want a clean start.

## 2. A five-minute tour without data

### 2.1 The offline demo

```
py -3.12 -X utf8 -B -m trading_lab offline-demo
```

It runs in under a second, makes no network request and writes no file. It feeds five synthetic situations to `trading_lab/risk.py`, a small deterministic check for simulated (paper) candidates, not the risk logic of the live executor, and prints:

```json
{
  "synthetic": true,
  "network_requests": 0,
  "live_execution_enabled": false,
  "cases": {
    "eligible_paper_candidate": "eligible_for_paper_review",
    "stale_data": "stale_or_future_state",
    "live_mode": "live_disabled",
    "oversized_candidate": "size_or_stop_limit",
    "daily_loss_cap": "daily_loss_limit"
  }
}
```

### 2.2 The strategy workshop

`tools/strategy_workshop.py` is the free toolkit for strategy authors. It checks a recipe's format, explains its rules and replays it on synthetic markets. A recipe is a JSON file with declarative rules, not code. Try it on the example `03_For_Client_Deliverables/AUTHOR_RECIPE_EXAMPLE_V8.json`, a mechanical long/short recipe in format version 2 that was written as a test fixture, not as a profitable strategy. Each command takes about a second.

```
py -3.12 -X utf8 -B tools/strategy_workshop.py validate 03_For_Client_Deliverables/AUTHOR_RECIPE_EXAMPLE_V8.json
py -3.12 -X utf8 -B tools/strategy_workshop.py lint 03_For_Client_Deliverables/AUTHOR_RECIPE_EXAMPLE_V8.json
py -3.12 -X utf8 -B tools/strategy_workshop.py test 03_For_Client_Deliverables/AUTHOR_RECIPE_EXAMPLE_V8.json --output runtime/trial.json
```

What you should see:

- **validate** prints `"valid": true` and `"recipe_sha256": "d0c86319a87666674e7862174352a1d553944c983344da34ced878e2d1d8fcd7"`. The sha256 is the recipe's identity: any change to its content, descriptive fields included, gives a new hash.
- **lint** prints the normalized rules, one warning (`authored_hold_unbounded`: the recipe sets no maximum holding time, so the end of the entry window never closes its position) and two limitations: the check does not prove that the numeric conditions can ever be met, and a format check or a synthetic example is not evidence of an edge.
- **test** prints a one-line summary with `"status": "COMPLETE"`, `"complete_runs": 3`, `"total_runs": 3` and `"trades": 4`, and saves the full result (about 1.2 MB) to `runtime/trial.json`. The synthetic suite has three made-up markets (rising, falling and sideways), each with 15 minutes in which entries are allowed and then time for exits. It shows that the rules run, not that they earn. Every attempt, failed ones included, is also logged in `runtime/trial_history/`.

### 2.3 The arena on a synthetic market

```
py -3.12 -X utf8 -B tools/preview_arena.py --coliseum-proof 6
```

Within about 10 seconds it prints:

```
http://127.0.0.1:8766/arena — disposable synthetic fixture
```

Open that address in a browser. You see the arena viewer, the same page that showed the team's real rounds, filled with six made-up participants in fixed states: three hold positions and stand inside the ring; the others wait, have finished or show an unconfirmed position, and stand outside. A seventh participant, Cash (labelled "Скриня", the chest), never trades and serves as the benchmark. A yellow banner at the top says, in Ukrainian, that this is a UI test with a synthetic market and a stub instead of the model API. The pages `/workshop`, `/review` and `/league` are served as well.

What the preview does and does not do:

- It copies `web/` into a temporary folder and starts the real panel server on 127.0.0.1 with a synthetic price feed, the fake exchange from `tests/test_exchange_lab.py` (so the `tests/` folder must be present) and a fake decision model. The exchange Start, Stop and Resume, the old one-trade Play button and balance reads are replaced by functions that refuse; a PAPER season started there runs on the synthetic feed.
- It writes nothing into the repository and stops by itself after 900 seconds. Press Ctrl+C to stop it earlier.
- Options: `--port` (default 8766), `--seconds`, and `--coliseum-proof 2`, `12` or `24` for other roster sizes.

## 3. Reproduce H10-ETH

H10-ETH buys ETH after a sharp one-hour rise while the price is above its 20-day average, with a passive limit order, and holds for up to a day. It passed gate K on the development data with a thin margin and never completed its forward test. Its card is [strategies/sonnet-h10-eth/](../strategies/sonnet-h10-eth/); the gates are explained in [METHODOLOGY.md](METHODOLOGY.md).

Here you rerun the official evaluator over the whole development period (DEV: 2020-01-01 to 2026-10-02 18:31 UTC) of the ETH-USDT-SWAP perpetual and compare the result with the recorded one. Plan for about 0.75 GB of disk, an unattended download of an hour or more, and 2 to 3 minutes of evaluation.

### 3.1 Download the public ETH history

```
py -3.12 -X utf8 -B tools/public_history.py fetch --inst ETH-USDT-SWAP --start 2020-01-01 --cutoff 2026-10-02T18:32:35.865Z --out data/okx/ETH-USDT-SWAP/1m
py -3.12 -X utf8 -B tools/public_history.py verify --out data/okx/ETH-USDT-SWAP/1m
```

- **Access.** The tool sends only GET requests to OKX's public market endpoints (`openapi.okx.com`). No account or key is involved. It does not check whether OKX is reachable from your country.
- **Cutoff.** `--cutoff` is the team's DEV cutoff: only candles closed by then are kept, so the last candle opens at 18:31 UTC.
- **Output.** One CSV of 1-minute candles, a CSV of funding rates if OKX still returns any for the period, a `MANIFEST.json` with hashes and gaps, and a per-day `cache/` folder that lets an interrupted download resume where it stopped. `verify` should print `{"ok": true, "problems": []}`.
- **Size.** About 3.55 million candles. The CSV and the cache are of similar size; the team's ETH folder took about 750 MB.
- **Time.** The tool gets 100 candles per request and paces itself at 8 requests per second by default (`--rate`, at most 9), so the full history takes about 37,000 requests, roughly 1.3 hours if nothing slows it down. This is an estimate from the code, not a measurement. You can stop and restart at any time; finished days are not downloaded again.
- **Funding.** OKX's public funding endpoint returns only about the last three months, so a download made today contains little or no funding history inside DEV. The evaluator then charges every 8-hour funding settlement it has no record for at a conservative rate, at least 1 bp of the position against you. The team also used OKX's monthly funding archive (from September 2021); this repository has no script to download it. This is the main reason your net result will be slightly different from the recorded one (section 3.5).

### 3.2 Build the store catalog

```
py -3.12 -X utf8 -B tools/team_data.py catalog --root data/okx/ETH-USDT-SWAP --inst ETH-USDT-SWAP
py -3.12 -X utf8 -B tools/team_data.py verify --root data/okx/ETH-USDT-SWAP
```

`catalog` scans every candle and funding CSV under the root (it skips `cache/` folders), checks the header, the minute grid, the order of rows and the gaps, and writes `CATALOG.json` and `SHA256SUMS.json` into the root. The evaluator reads only catalogued files. For the full download, the printed coverage should show `first_open_utc` 2020-01-01T00:00:00Z, `last_open_utc` 2026-10-02T18:31:00Z, 3,552,152 rows and no missing minutes, the same as the team's copy, and `problems` should be empty. `verify` re-hashes every file and should print `{"ok": true, "problems": []}`. Each command should take about a minute or less.

`SHA256SUMS.json` is written only when it does not exist yet. If you add or replace files in the store later, delete it and run `catalog` again; otherwise `verify` reports the new files.

### 3.3 Run the official evaluator

```
py -3.12 -X utf8 -B tools/team_eval.py run --recipe strategies/sonnet-h10-eth/recipe.json --store data/okx/ETH-USDT-SWAP --author you --label H10-ETH-repro --out runtime/eval/h10_dev
```

- `--store` points at the catalogued instrument folder. Pass it explicitly: the default depends on the recipe format.
- The data set defaults to `--set dev`. In this mode the evaluator never reads data after the DEV cutoff.
- H10 is a format-4 recipe, so the evaluator runs the candle engine `tools/team_eval_v4.py` in one process. For the whole ETH history this took the team 2 to 3 minutes and less than 1.5 GB of memory. The tool refuses to start when less than 3 GB of RAM is free (it cannot read free memory on macOS and skips the check there).
- It prints a one-line summary (verdict, net, stress, closed trades, elapsed time) and writes three files to `--out`: `ledger.json` with every trade and execution, `gates.json` with all metrics and checks, and `gates.md`, a readable report. It also appends one line to `runtime/team_eval/you/registry.jsonl` (change it with `--registry`), so that no attempt disappears.

### 3.4 What gates.md shows

From top to bottom:

1. **Header:** recipe name and `recipe_sha256`, short hashes of the evaluator and of the engine code, the span and the number of one-day windows, overlapping trades dropped, positions force-closed at the end of the data, the funding scenario used for the verdict, and the verdict itself.
2. **Checks:** one row per gate check with its value, its rule and PASS, FAIL or NOT_EVALUATED. The checks are explained in [METHODOLOGY.md, section 4](METHODOLOGY.md#4-gate-k-the-historical-test).
3. **Scenarios:** net and stress results under the base, conservative (verdict) and, for strategies that hold longer than 8 hours on average, sensitivity funding scenarios, with the funding charged for missing settlements, trades, win rate and drawdowns.
4. **Costs and exits:** fees, mean holding time, worst move inside a trade, and exits by reason (time, stop, target, end of data).
5. **Epochs:** E1 (2020 to 2023), E2 (from 2024) and the E2 tail (from July 2025) with coverage, trades, net, stress and active quarters.
6. **Years and quarters** by entry date.
7. **Format-4 notes:** execution model `v4-path5s-2`, a stop-slippage stress, entries skipped because the size was below one lot, the funding share of net and, for recipes with passive (post-only) entries like H10, a grid of maker-fill assumptions down to "every maker fill charged as a taker". The same line labels the recipe "EVAL_ONLY (Live/Demo/PAPER refuse it in this release)". That label is out of date: Demo and Live run format-4 recipes in RULES_ONLY mode (section 6), and PAPER runs the BTC ones.
8. A closing reminder that this is model evidence: candle-based quotes, not a market recording and not proof of an edge.

### 3.5 Compare with the strategy card

The card [strategies/sonnet-h10-eth/README.md](../strategies/sonnet-h10-eth/README.md) records the team's result:

| | Recorded |
|---|---|
| recipe_sha256 | `dcf486662716f1b7a986c0e9f2f0d50d93d6fba8ad928d81f73541e77f906a81` |
| Closed trades, orders | 550, 639 |
| Exits | 431 by time, 34 at target, 85 at stop |
| Net / stress, whole DEV | +30.81 / +25.33 USDT on a 100 USDT account |
| E2 | 200 trades, net +6.68 USDT, 7 of 11 active quarters positive |
| E2 tail | 93 trades, net +6.03 USDT |

What should match and what should not:

- **Trades.** H10 reads only candles, so the same candles should give the same 550 trades with the same entry and exit times. If they do not, compare your catalog coverage (3,552,152 rows, no gaps) first.
- **Net and stress.** Slightly lower than recorded, because of funding (section 3.1). The team's earlier official run, with funding data only from 2026-07-27, gave +30.57 net and +25.09 stress on the same trades; expect numbers close to that. The exact value depends on how much real funding history your download holds.
- **Hashes.** `ledger_trades_sha256` in `gates.json` (recorded: `3f26c2e7ee0fda5469e413fd35f9bed0cd1c50d920ecb8d97a0e36647777cac9`) includes the funding of each trade, so it matches only with identical funding data. The evaluator and engine-code hashes in the header will differ from the recorded ones because some public files differ from the team's copies, for example in default paths. Compare numbers, not those hashes.
- **Verdict.** Without `--prereg` and `--trials` the verdict reads `NOT_ALL_CHECKS_EVALUATED`: the paperwork check is NOT_EVALUATED, while every numeric check should show PASS. The author's own run passed both files and got PASS.

A PASS here means the recipe survived the backtest gate. It does not mean it makes money: see the card's "Why it is not enough" and [RESULTS.md](RESULTS.md).

## 4. Test your own idea

This is the workflow the team followed. [METHODOLOGY.md](METHODOLOGY.md) explains why each step exists.

### 4.1 Write the recipe

The format is described in [RECIPE_SPEC.md](RECIPE_SPEC.md); [strategies/sonnet-h10-eth/recipe.json](../strategies/sonnet-h10-eth/recipe.json) is a complete example. The official evaluator runs format-4 recipes on BTC-USDT-SWAP, ETH-USDT-SWAP and SOL-USDT-SWAP with positions held for at most 24 hours (`max_hold_seconds` up to 86,400). Recipes in the older formats 1 and 2 trade BTC only and run on the slower exact engine: 30 to 60 minutes for the whole DEV, depending on the number of worker processes (32 minutes on 6 workers in one run), and it stops whenever free RAM falls below 6 GB. Check your file until both commands pass:

```
py -3.12 -X utf8 -B tools/strategy_workshop.py validate my_recipe.json
py -3.12 -X utf8 -B tools/strategy_workshop.py lint my_recipe.json
```

The workshop's `test` command does not run format-4 recipes (it stops with a generic error); the training run on the panel's `/workshop` page does (ETH and SOL recipes on synthetic markets only). For format-4 recipes the evaluator is the real test. Download the history of your instrument as in section 3.1 (BTC and ETH data start on 2020-01-01; SOL starts on 2021-01-22).

If your recipe uses the premium or basis features, also download index and mark-price candles and catalogue them next to the swap store; without them those features stay UNKNOWN and the recipe never enters ([RECIPE_SPEC.md, section 8](RECIPE_SPEC.md#8-unknown-values-and-data-coverage)). For ETH:

```
py -3.12 -X utf8 -B tools/public_history.py fetch --kind index --inst ETH-USDT --rate 4 --start 2020-01-01 --cutoff 2026-10-02T18:32:35.865Z --out data/okx/ETH-USDT_index/1m
py -3.12 -X utf8 -B tools/team_data.py catalog --root data/okx/ETH-USDT_index --inst ETH-USDT
py -3.12 -X utf8 -B tools/public_history.py fetch --kind mark --inst ETH-USDT-SWAP --rate 4 --start 2020-01-01 --cutoff 2026-10-02T18:32:35.865Z --out data/okx/ETH-USDT-SWAP_mark/1m
py -3.12 -X utf8 -B tools/team_data.py catalog --root data/okx/ETH-USDT-SWAP_mark --inst ETH-USDT-SWAP
```

`--rate 4` stays under OKX's lower request limit for index candles (10 requests per 2 seconds); the mark-price command uses it too, to be safe. Each of these downloads takes about twice as long as the swap candles. The team's index and mark data start on 2020-01-02 16:00 UTC.

### 4.2 Pre-register before the first run

Before you look at any result, write a plan, for example `research/my-idea/PREREG.md`:

- the hypothesis and the mechanism, why it should still work after 2024, and what result would refute it;
- exactly what you will test: every configuration (the team allowed at most 24 per research cycle);
- the rule that will pick the winner;
- the controls: long and short separately, random entries in the same market regime, the signal shifted in time as a placebo, how many neighbouring configurations also work, and the gross move per trade against the cost of about 14 bps per round trip (24 bps under stress).

Commit the plan to Git, together with the first line of an append-only log `research/my-idea/TRIALS.jsonl`, in its own commit, before any run. The commit time is your evidence. Add one line for every later run, check and verdict, failures included, for example:

```json
{"at": "2026-11-18T09:00:00Z", "kind": "prereg", "file": "research/my-idea/PREREG.md", "sha256": "<sha256 of the file>"}
```

### 4.3 Run gate K, looking at the tail once

Choose the configuration on data up to 2025-06-30 only, by your pre-registered rule. Then run the chosen one once over the whole DEV, which adds the last five quarters (the tail) that you have not looked at:

```
py -3.12 -X utf8 -B tools/team_eval.py run --recipe my_recipe.json --store data/okx/ETH-USDT-SWAP --end 2025-06-30 --author me --label C01-select --prereg research/my-idea/PREREG.md --trials research/my-idea/TRIALS.jsonl --out runtime/eval/C01-select
py -3.12 -X utf8 -B tools/team_eval.py run --recipe my_recipe.json --store data/okx/ETH-USDT-SWAP --author me --label C01-dev --prereg research/my-idea/PREREG.md --trials research/my-idea/TRIALS.jsonl --out runtime/eval/C01-dev
```

- With both paperwork files given, the paperwork check passes when the log is not empty. The tool records their sha256 in the ledger; whether their content is honest is for you, or a reviewer, to check.
- If the tail fails, close the hypothesis. Do not re-tune on the tail.
- `gates --ledger <ledger.json>` recomputes the gates from a saved ledger without rerunning the engine.
- `clone --a <your ledger.json> --b <other ledger.json>` checks whether your strategy is a near copy of another one: more than 50% of entries within 30 minutes of the other's entries, or a daily PnL correlation above 0.8.

A PASS makes a recipe a candidate, nothing more. On synthetic data the long-horizon version of the gate still lets about 1 to 2% of strategies without an edge through, and the standard gate was never calibrated that way (METHODOLOGY section 7).

### 4.4 Long holds: the random-entry control

If your strategy holds positions for days or weeks, it will make too few trades for gate K, and on the 2020 to 2025 history most trend profits came from the market rising: in one author's test, random long entries with the same holding times earned about three quarters as much as the real entries (462 against 622 bps per trade). The long-horizon track tests timing instead. It compares your real entry and exit dates with the same trades moved to every other date of the period (circular shifts). Buy-and-hold, or any random choice of dates, gets a p-value around 0.5.

The official evaluator cannot hold format-4 positions for longer than 24 hours, so simulate the strategy with your own code and give the tool a trade list:

```
py -3.12 -X utf8 -B team/judge/tools/gate_k_long.py --trades my_trades.csv --instrument ETH-USDT-SWAP --configs 4 --store data/okx/ETH-USDT-SWAP --out runtime/kt/my-idea
```

- `my_trades.csv` columns: `entry_at` and `exit_at` (ISO time in UTC), `side` (`long` or `short`), `notional_usdt` (entry size on a 100 USDT account), `net_usdt` (after all costs and funding), and optionally `executed_notional_usdt` (default: twice the notional). Trades must not overlap: one account, one position.
- `--configs` is the number of configurations your PREREG declared (default 24, the team's cap). The timing threshold on DEV is 0.05 divided by that number.
- The tool reads daily closes from the full DEV store of the instrument, prints a one-line summary and writes `gate_k_long.json` and `gate_k_long.md` with every check and the p-values by period.
- It applies only to strategies with fewer than 100 closed trades on DEV and a median hold of at least 72 hours; others get NOT_ELIGIBLE and belong to the standard gate K. Results from your own trade list are labelled INDICATIVE.
- To use the family form, which does not penalize many similar configurations, add `--family <files or folder>` with the trade lists of every configuration declared on this instrument, and `--instruments <q>`.

The checks and their calibration are in [METHODOLOGY.md, sections 5 and 7.1](METHODOLOGY.md#5-track-k-t-gate-k-for-long-holding-periods).

### 4.5 Forward test

A backtest pass is not proof. Freeze the recipe (its sha256 is its identity), declare in advance how many trades will decide (the team used 30), and count only trades on market data after the freeze. Fetch those candles into a separate store (section 3.1 with `--start` at the freeze time and a recent `--cutoff`, for example into `data/forward/ETH-USDT-SWAP/1m`) and catalogue it as in section 3.2. Then run `team_eval.py run --set forward --store data/forward/ETH-USDT-SWAP --start <first window> --declared-n 30` and the independent cross-check `team/judge/tools/gate_d_first_n.py --ledger <its ledger.json> --n 30`. Keep the DEV store at `data/okx/<instrument>`: forward runs read their feature warm-up from there, so no code change is needed. The procedure is in [METHODOLOGY.md, sections 6 and 10.4](METHODOLOGY.md#6-gate-d-the-forward-test). With a few trades a week this takes months, which is the honest price of a forward test.

## 5. Run the tests

```
py -3.12 -X utf8 -B -m unittest discover -s tests
node --test tests/*.cjs tests/v8_ui/*.test.js
```

- **Python:** 118 test files and about 1,740 tests. A full run takes about 30 to 35 minutes on a laptop (the team's full runs took 32 to 34 minutes). Add `-v` to see each test's name. numpy must be installed. On Windows, with Node.js and Pillow installed, a complete run ends with `OK` and one skipped test.
- **Node:** 474 UI tests in about 2 seconds, using only Node's built-in test runner. In PowerShell and Command Prompt the wildcards are expanded by Node itself, which needs Node 21 or newer; with an older Node, run the command from a shell that expands wildcards (bash, zsh, Git Bash).
- Everything runs offline, with fake exchanges and a fake model transport. No test needs keys. Tests write to temporary folders, apart from a small lease file under `runtime/research_resources/`.
- Some tests are skipped when Windows PowerShell, Pillow or a local data fixture that is not in the repository is missing. A few Python tests also need Node.js; two of them fail without it, the others are skipped.
- `tests/test_arena_jev.py` once failed in the team's runs while the machine was under heavy load and passed when run again on its own.

## 6. Demo and Live: real orders

> [!WARNING]
> This section is about sending orders to a real exchange. Nothing in sections 1 to 5 needs it. Our own real-money result was a loss: 86 trades lost 1.65 USDT, mostly to fees. Nothing here is investment advice. If you go ahead, start with OKX demo trading, use a dedicated subaccount that holds only money you are ready to lose, use a key without withdrawal rights, and write down your stop rule before the first trade. A stop order limits a loss but does not guarantee it: in a fast market the fill can be worse than the stop price.

### 6.1 What happens by default: nothing trades

Checked in the code:

- `web_panel.py` only starts a local web server on 127.0.0.1 (port 8765) and prints that no trade starts automatically. Opening or refreshing a page never places an order.
- Every action that could trade is a POST request. The server accepts it only from the same computer, from its own page (same Origin) and with a random token that it puts into the page when it starts.
- `one_trade.py` does nothing unless you pass `--check` (read-only checks) or `--live`.
- The exchange executor checks every request against fixed lists of allowed endpoints and refuses every POST until a Start or Resume has passed the account checks and armed it. The code has no withdrawal or transfer endpoint.
- No keys come with the repository. OKX keys are read only from local files that you create yourself (the old one-trade bot also accepts environment variables), and the paid model's key from `.env.local` or the environment.
- Tests and previews use fake exchanges.

To place an order you have to do every step of one of the three paths below.

### 6.2 OKX Demo (virtual money): start here

1. In OKX, switch to demo trading and create a demo API key with Read and Trade permissions. The account must be in futures mode (`acctLv` 2) with net position mode.
2. Create the file `.env.okx.demo.local` in the repository root:

   ```
   OKX_SITE=global
   OKX_ENV=demo
   OKX_API_KEY=...
   OKX_API_SECRET=...
   OKX_API_PASSPHRASE=...
   ```

   `OKX_SITE` is `global`, `eea`, `us` or `tr`, whichever OKX API host your account uses. These local key files must never be committed.
3. Start the panel with `py -3.12 -X utf8 -B web_panel.py --open` and go to `http://127.0.0.1:8765/workshop`.
4. Add your recipe as a version.
5. In the block "Проба на біржовому демо" (exchange demo run), press "Перевірити доступ · лише читання" (check access, read only). This sends GET requests only.
6. Choose the budget, the entry window (one of seven, from 5 minutes to 24 hours) and the execution mode ("Що перевіряємо", what we test), and press "Запустити OKX Demo" (start OKX Demo). The executor now sends real orders to OKX's demo environment. Check the mode first: the form preselects "Стратегія · з Jev" (shared_jev), in which a paid third-party LLM decision model, called Jev in the code (`typesafe/jev-1.13`, reached through OpenRouter), must approve each entry or add. It needs `OPENROUTER_API_KEY` in a file `.env.local` (or in the environment) and costs money. Choose "RULES_ONLY · Jev SHADOW" to let the recipe decide alone with no model calls; format-4 recipes run only in this mode. A third mode, "Механіка OKX Demo · без Jev", is offered only for the unchanged Demo test recipes in `03_For_Client_Deliverables/`.

### 6.3 Arena Live (real money)

1. An OKX account where USDT perpetual swaps are available to you, in futures mode (`acctLv` 2) with net position mode.
2. One dedicated subaccount per bot. The check refuses the main account. Fund it with USDT you are ready to lose, at least the bot's budget plus 1%; it must have no open position in the contract and no open orders.
3. An API key on that subaccount with Read and Trade permissions only. A key that can withdraw is refused for Live.
4. One key file per subaccount in the repository root, `.env.okx.arena-a.local` (and `.env.okx.arena-b.local` for a second bot), with the same five lines as above and `OKX_ENV=live`. More profiles can be declared in `LocalSettings/exchange_accounts.json`. Environment variables are ignored for these profiles, and demo and live keys must be in different files.
5. Start the panel and open `http://127.0.0.1:8765/workshop`. Add the recipe, run a free training run ("Навчальний прогін"), freeze it ("Зафіксувати правила") and promote it ("Додати в PAPER-арену"). Live starts only recipes from the arena catalogue (the built-in samples and the recipes you promoted) whose sha256 is intact; recipes on the Live deny list (`trading_lab/live_denylist.py`) are refused.
6. Open `http://127.0.0.1:8765/arena` and press "Керування" (control). In the dialog "Керування ареною" (arena control), section "Реальний сезон" (real season), pick the recipes and one profile for each, and press "Перевірити доступ · лише читання". Every account must pass.
7. Set the budget (25 to 500 USDT per bot, at most 1,000 USDT in total), the entry window (one of seven, from 5 minutes to 24 hours) and the execution mode, and press "Запустити реальний сезон" (start real season). As in Demo, the mode list preselects the paid Jev gate ("Jev-гейт · чинний режим"); choose "RULES_ONLY · Jev SHADOW" for no model calls, which format-4 recipes require. Every bot trades with isolated margin at 1×; a higher leverage can be chosen only for recipes listed in `trading_lab/live_leverage.py`, up to their limit.

From then on the executor sends real orders. After every fill it places a reduce-only stop order on the exchange; the panel's Stop closes a bot's position with a reduce-only order. Keep the computer and the panel running until the round has finished.

### 6.4 The old one-trade bot (real money, BTC only)

This is the project's first execution test, kept because the tests and the `/live` page use it.

1. An OKX API key with Trade permission on an account in futures mode with net position mode, with no open position or order and at least 10.5 USDT available.
2. The file `.env.okx.local` in the repository root (or the same five variables in the environment) with `OKX_ENV=live`. When `OKX_ENV` is missing it means live; the bot refuses demo.
3. `OPENROUTER_API_KEY` in `.env.local` or in the environment. The bot asks the paid Jev model before it enters and places no order without its approval.
4. Run `py -3.12 -X utf8 -B one_trade.py --live` (the private repository also had a Windows double-click launcher for this bot; it is deliberately not included here), or press Play on `http://127.0.0.1:8765/live`, which runs `one_trade.py --live --strategy monitored`.

The plain bot buys BTC-USDT-SWAP for at most 10 USDT at isolated 1× and closes the position with a reduce-only order; the monitored variant makes up to 3 entries, at most 30 USDT in total, within at most 5 minutes. A lock file and a journal in `runtime/` prevent a second bot and a repeated trade: starting it again only recovers or closes the cycle already begun. Ctrl+C asks the bot to close the position; killing the process cannot. `one_trade.py --check` runs the same account checks with GET requests only.

### 6.5 Other paid calls

`py -3.12 -X utf8 -B -m trading_lab jev-smoke --paid-once` and `jev-okx-review --paid-once`, `tools/jev_research.py` and `tools/jev_gate_probe.py --paid` call the paid Jev model through OpenRouter but place no orders. The PAPER arena on `/arena` simulates trades on public OKX prices without keys; only its optional shared_jev mode is paid. Every paid path has a spending cap stored in a local SQLite ledger under `runtime/`.
