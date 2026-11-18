# Strategy recipe format (schema 4)

A strategy in this repository is a JSON **recipe**: a fixed, declarative description of when to open a position and where to close it. This document specifies format version 4 ("schema 4"), the format of the project's later strategies, including H10-ETH, the only one that passed the backtest gate. Use it to write a recipe, to read the published ones in [`strategies/`](../strategies/), or to check either with the tools in this repository.

- The validator in `trading_lab/strategy_v4.py` is the source of truth. If this text and the code disagree, the code wins.
- Version labels: semantics `strategy-v4-early-1`, feature catalogue `v4-closed-bar-2`, evaluator execution model `v4-path5s-2`. Code comments also call this format "SDK v3"; its schema number is 4.
- A recipe that validates is well-formed. That says nothing about whether it makes money. [METHODOLOGY.md](METHODOLOGY.md) describes the tests a recipe had to pass, and [RESULTS.md](RESULTS.md) what happened.

This text was checked against the code in this repository in November 2026. The `validate` and `lint` outputs shown below were produced by that code.

## Contents

1. Tools that read a recipe
2. Example 1: a minimal valid recipe
3. Top level
4. Rules
5. Entry predicates
6. Timing
7. Feature catalogue
8. UNKNOWN values and data coverage
9. Orders, size, exits and costs
10. Engines versus the evaluator
11. Validation errors
12. Validator output and the recipe hash
13. Example 2: H10-ETH, field by field
14. Checklist for a new recipe

## 1. Tools that read a recipe

Run the commands from the repository root. They use the Windows launcher `py -3.12 -X utf8 -B`; on macOS or Linux use `python3.12 -X utf8 -B`.

| Tool | What it does with a schema-4 recipe |
|---|---|
| `tools/strategy_workshop.py validate <file>` | Runs the validator and prints the recipe hash. Offline and free. |
| `tools/strategy_workshop.py lint <file>` | The same validator, plus the list of features the recipe reads. It has no further checks; its `warnings` list is always empty. |
| `tools/team_eval.py run --recipe <file> ...` | The official evaluator. It runs the same validator, replays the recipe on 1-minute OKX history with the execution and cost model of section 9, and computes the gates. Offline; it needs a data store (see [QUICKSTART.md](QUICKSTART.md) and [METHODOLOGY.md](METHODOLOGY.md), section 10). |
| The web workshop and the arena (the panel started by `web_panel.py`) | Admit a recipe only if it also passes the engine admission checks of section 11.2. Then they run it on replays (workshop), with simulated fills on live BTC quotes (PAPER), or on OKX demo and real accounts (Demo, Live). |

- No language model is called when a schema-4 recipe runs: the engines run it rules-only (`RULES_ONLY`).
- The command-line workshop's `test`, `explain` and `lots` commands do not support schema 4. Given a schema-4 recipe, they stop with the generic error message.
- The older formats (recipes without a version number, and schemas 2 and 3) are still accepted for the project's historical strategies. This document covers schema 4 only.

## 2. Example 1: a minimal valid recipe

```json
{
  "strategy_schema_version": 4,
  "name": "Format example: buy after a falling hour",
  "author": {
    "name": "Example",
    "kind": "example",
    "model": "",
    "submitted_at": "",
    "prompt": "",
    "attempts": 1,
    "edited_by": ""
  },
  "description": "Teaching example of the recipe format. It was never evaluated and is not a strategy recommendation.",
  "original_text": "Buy BTC when the last closed hour fell by more than 1 %. Exit at a 1 % stop, a 2 % target or after 4 hours, whichever comes first. After an exit, wait 1 hour.",
  "confirmed": true,
  "instrument": "BTC-USDT-SWAP",
  "rules": {
    "side": "long",
    "entry": {
      "when": {"feature": {"name": "return_bps", "tf": "1h", "n": 1}, "op": "lt", "value": -100}
    },
    "order": {"type": "taker"},
    "size_pct": 20,
    "stop_loss_bps": 100,
    "take_profit_bps": 200,
    "max_hold_seconds": 14400,
    "cooldown_seconds": 3600
  }
}
```

This is a format example, not a strategy. It was never run through the evaluator, and nothing here suggests that it would make money. It is minimal in the sense that every tool accepts it with nothing extra: one comparison, a taker order and the full passport. The validator alone would also accept `"author": {}`, but the workshop and the arena would not.

Save it as `minimal_recipe.json` and check it:

```
py -3.12 -X utf8 -B tools/strategy_workshop.py validate minimal_recipe.json
```

Output:

```json
{
  "valid": true,
  "name": "Format example: buy after a falling hour",
  "recipe_sha256": "c81e6abf7b75fd1720e38e7396a9fdbc02297f54d95293709727edd7d8ecc18a",
  "strategy_schema_version": 4,
  "engines": "workshop, PAPER, Demo and Live (RULES_ONLY)"
}
```

`lint` prints the same fields plus `"features": ["return_bps@1h:1"]`, an empty `warnings` list and two fixed notes: the official gates come from `tools/team_eval.py`, and formal validation is not evidence of edge. The `engines` line is derived from the instrument alone. It does not check the passport or the text limits of section 11.2, and it is not a suggestion to trade the recipe.

Field by field:

- `strategy_schema_version: 4` selects this format.
- `name`, `description`, `original_text`: plain text. `original_text` is the strategy in the author's own words; `rules` formalise it. Keep them within the engine limits of 60, 1,000 and 16,000 characters.
- `author`: the seven-key passport of section 3.1. Its `kind` is `"example"`, so `model`, `submitted_at` and `prompt` may be empty strings.
- `confirmed: true`: the author states that `rules` match `original_text`.
- `instrument`: the OKX BTC-USDT perpetual swap.
- `rules.side: "long"`: buy only.
- `rules.entry.when`: a single comparison. `return_bps` on `1h` with `n` = 1 is the move from the close of the hourly bar before the last one to the close of the last closed hourly bar, in basis points (1 bps = 0.01 %). Below -100 means that the last full hour fell by more than 1 %.
- Entry rules are level-triggered (section 5.3). While the account is flat, the rule is evaluated on every new closed minute, and an hourly feature keeps its value until the next hourly bar closes, so one falling hour gives a TRUE verdict at 60 decisions in a row. The first of them enters. The one-hour cooldown then makes sure that the next decision already sees a newer hourly bar.
- `rules.order: {"type": "taker"}`: the order goes to the book 5 seconds after the decision, at the ask plus 2 bps of slippage, with a 5 bps fee (section 9).
- `rules.size_pct: 20`: each entry may use 20 USDT, 20 % of the evaluator's fixed 100 USDT account, rounded down to whole lots of 0.0001 BTC at the fill price. At an ask of 100,000 USDT the fill price is 100,020 and one lot costs 10.002 USDT, so 20 USDT buys one lot, not two.
- `rules.stop_loss_bps: 100`, `take_profit_bps: 200`, `max_hold_seconds: 14400`: close at -1 % or +2 %, measured on executable prices (section 9.6), or after 4 hours.
- `rules.cooldown_seconds: 3600`: wait one hour after an exit.

Any change to the content, even one character of a text, gives a different `recipe_sha256` (section 12).

### When validation fails

`tools/strategy_workshop.py` then prints only `Recipe/dataset validation failed; training attempts remain in local history.` and exits with code 1. To see the reason, call the validator directly:

```
py -3.12 -X utf8 -B -c "import json, sys; from trading_lab.strategy_v4 import validate_payload; print(validate_payload(json.load(open(sys.argv[1], encoding='utf-8-sig')))['recipe_sha256'])" minimal_recipe.json
```

For a valid recipe it prints the hash. For an invalid one it prints a traceback whose last line names the reason, for example `trading_lab.strategy_v4.ContractError: hold_and_cooldown_whole_minutes`. `tools/team_eval.py run --recipe <file>` reports the same code, as `v4 recipe rejected: <code>`, before it reads any market data. Section 11.1 lists the codes.

To run the engine admission checks as well:

```
py -3.12 -X utf8 -B -c "import json, sys; from trading_lab.arena_strategies import validate_strategy; print(validate_strategy(json.load(open(sys.argv[1], encoding='utf-8-sig')))['v4_recipe_sha256'])" minimal_recipe.json
```

Its error messages are in Ukrainian; section 11.2 translates them.

## 3. Top level

A recipe is one JSON object in UTF-8 (a byte-order mark is accepted), at most 131,072 bytes. It is fully declarative: boolean trees over a fixed catalogue of closed-bar features, plus fixed risk numbers. There is no code and no free arithmetic; the only arithmetic is `mult × feature`, inside one comparison or in a post-only offset (section 9.1).

A recipe trades one instrument with one net position: one entry per position, no adds, no partial exits, and no reversal while a position is open. The evaluator models no leverage: a position's notional is at most `size_pct` USDT of its 100 USDT account (section 9.5), as with isolated margin at 1x. Platform code owns timing, orders and risk; the recipe says only when to enter and where to exit.

The object has exactly these eight keys. Any other key rejects it, including the fields that the validator adds to its own output (section 12).

| Key | Type | Validator | Engines (section 11.2) |
|---|---|---|---|
| `strategy_schema_version` | number | `4` | |
| `name` | string | 1-200 characters | 1-60 characters |
| `author` | object | any object | the seven-key passport of 3.1 |
| `description` | string | 1-20,000 characters | 1-1,000 characters |
| `original_text` | string | 1-20,000 characters | 1-16,000 characters |
| `confirmed` | boolean | must be `true` | |
| `instrument` | string | `"BTC-USDT-SWAP"`, `"ETH-USDT-SWAP"` or `"SOL-USDT-SWAP"` | |
| `rules` | object | section 4 | |

- `original_text` is the strategy in the author's own words; `rules` formalise it. `confirmed: true` states that the two match.
- The engines also need every text, the passport included, to be clean already: no leading or trailing whitespace of any kind (space, tab, newline, no-break space), and no control characters below U+0020 other than newline, tab and carriage return. They never trim or repair a recipe; one stray space rejects it.

### 3.1 The author passport

The validator accepts any JSON object as `author`. The workshop, the arena and the exchange engines accept only this seven-key passport, and the recipe hash covers it.

| Key | Content | Engine rule |
|---|---|---|
| `name` | the author's display name | string, 1-60 characters |
| `kind` | `"human"`, `"model"` (an AI model wrote the recipe) or `"example"` (a teaching or technical example) | one of the three |
| `model` | the model name and version, as declared | string of at most 150 characters; must not be empty for `"model"` |
| `submitted_at` | when the recipe was written; UTC ISO 8601, such as `2026-11-18T12:00:00Z`, is the clearest | string of at most 60 characters; must not be empty for `"model"`; the format is not checked |
| `prompt` | the request that a model answered, named or quoted in one line | string of at most 8,000 characters; must not be empty for `"model"` |
| `attempts` | how many attempts the recipe took; `1` for a first answer | whole JSON number 1-1,000 (`1`, not `"1"` or `true`) |
| `edited_by` | who edited the recipe after the author, or `""` | string of at most 300 characters; may be empty |

Exactly these seven keys, in any order. Model names in a passport are declarations; nothing verifies them. Two examples:

```json
{"name": "<your name>", "kind": "human", "model": "", "submitted_at": "2026-11-18", "prompt": "", "attempts": 1, "edited_by": ""}
{"name": "<display name>", "kind": "model", "model": "<model name and version>", "submitted_at": "2026-11-18T12:00:00Z", "prompt": "Write one schema-4 recipe for ETH-USDT-SWAP following docs/RECIPE_SPEC.md", "attempts": 1, "edited_by": ""}
```

### 3.2 Instruments

| Instrument | Tick | Contract | Lot = minimum size | Runs in |
|---|---|---|---|---|
| `BTC-USDT-SWAP` | 0.1 USDT | 0.01 BTC | 0.01 contract = 0.0001 BTC | evaluator; workshop replays; PAPER; Demo; Live |
| `ETH-USDT-SWAP` | 0.01 USDT | 0.1 ETH | 0.01 contract = 0.001 ETH | evaluator; workshop synthetic market; Demo; Live |
| `SOL-USDT-SWAP` | 0.01 USDT | 1 SOL | 0.01 contract = 0.01 SOL | evaluator; workshop synthetic market; Demo; Live |

- The tick and lot values come from the instrument files the evaluator reads, `tools/okx_instrument_*.json`. OKX can change them.
- Both order types (section 9.1) are accepted everywhere. The workshop's synthetic market cannot place post-only orders, because it has no candles or tick to compute a limit from.
- The synthetic market is a mechanics check. While it admits entries, it has only about 30 to 45 minutes of candle history, so any feature that needs more stays UNKNOWN there and the recipe does not enter. The minimal example above, whose feature needs 2 hours, makes no trade on it.

## 4. Rules

`rules` has exactly these eight keys. There is no `exit`, `stop`, `target`, `trail`, `max_entries` or any other key.

| Key | Type | Allowed values (inclusive) | Meaning |
|---|---|---|---|
| `side` | string | `"long"`, `"short"` or `"both"` | the direction or directions the recipe may trade |
| `entry` | object | section 5 | when to enter |
| `order` | object | section 9.1 | how to enter: `taker` or `post_only` |
| `size_pct` | number | 5-30 | budget per entry, in % of the evaluator's 100 USDT account |
| `stop_loss_bps` | number | 20-1,500 | stop distance from the entry fill price |
| `take_profit_bps` | number | 40-3,000 | target distance from the entry fill price |
| `max_hold_seconds` | integer | 60-86,400, a multiple of 60 | maximum position age (at most 24 hours) |
| `cooldown_seconds` | integer | 0-86,400, a multiple of 60 | pause after an exit before the next decision |

Numbers must be finite JSON numbers; `true` and `false` are not numbers. Fractions are allowed unless an integer is required. 1 bps (basis point) = 0.01 %. Section 9 gives the exact meaning of the risk numbers.

## 5. Entry predicates

### 5.1 Shape

```text
"side": "long" or "short":   "entry": {"when": P}
"side": "both":              "entry": {"long": {"when": P_long}, "short": {"when": P_short}}
```

A predicate `P` is one of these nodes, each with exactly the keys shown:

```text
{"all": [P, ...]}                               1-8 children; TRUE if every child is TRUE
{"any": [P, ...]}                               1-8 children; TRUE if at least one child is TRUE
{"not": P}
{"feature": F, "op": OP, "value": x}            compares F OP x; x is a number in [-1e9, 1e9]
{"feature": F, "op": OP, "ref": G, "mult": m}   compares F OP (m × G); m is in [-10, 10], m ≠ 0
```

- `OP` is `"gt"` (>), `"gte"` (≥), `"lt"` (<) or `"lte"` (≤). There is no equality operator; build ranges with `all`.
- `F` and `G` are feature specs `{"name": <string>, "tf": <timeframe>, "n": <whole number>}` with exactly these three keys (section 7). The timeframe key is `tf`.
- Limits per predicate (per branch for `both`): depth at most 6, with the root at level 1; at most 64 nodes, where each `all`, `any`, `not` and leaf counts 1 and feature specs do not count.

Two leaves:

```json
{"feature": {"name": "utc_weekday", "tf": "1d", "n": 1}, "op": "lte", "value": 4}
{"feature": {"name": "return_bps", "tf": "1h", "n": 4}, "op": "gt", "ref": {"name": "realized_vol_bps", "tf": "1h", "n": 48}, "mult": 2}
```

The first is TRUE from Monday to Friday (UTC). The second is TRUE when the move over the last 4 closed hours is more than twice the standard deviation of hourly returns over the last 48 hours.

### 5.2 Three-valued logic

A leaf is TRUE, FALSE or UNKNOWN. It is UNKNOWN when `F` or `G` is UNKNOWN (section 8). Any UNKNOWN leaf makes the whole predicate UNKNOWN, also under `any` and `not`; there is no short-circuit. For example, `{"any": [TRUE, UNKNOWN]}` is UNKNOWN.

### 5.3 The entry verdict

- `long` or `short`: enter when `P` is TRUE. FALSE or UNKNOWN means no entry (HOLD).
- `both`: enter on a side when its branch is TRUE and the other branch is known and FALSE. Both TRUE (a conflict) or either UNKNOWN means HOLD. The check just before the order (section 6) must give the same side.
- Entries are level-triggered. While the account is flat, the rule is evaluated again on every new closed minute; there is no "crosses above" event. If the condition is still TRUE after an exit plus the cooldown, the recipe enters again.

## 6. Timing

- All times are UTC. Timeframes: `"1m"`, `"5m"`, `"15m"`, `"1h"`, `"4h"`, `"1d"`. Bars align to UTC: `4h` bars open at 00, 04, ..., 20 h, and a `1d` bar runs from 00:00 to 24:00.
- Bars are built from closed 1-minute swap candles. A bar is closed when its last minute has closed, and complete when all of its minutes are present.
- The decision close C is the close time of the newest closed 1-minute bar. That bar becomes visible at the first 5-second quote at or after C + 5 s. One decision is made per newly visible bar, and only while the account is flat, the cooldown is over and no order is resting.
- A feature at C uses only bars closed at or before C. "Price" in section 7 means the close of the 1-minute bar that ends at C. Features on longer timeframes change only when their bar closes, except those that use price (`sma_gap_bps`, `dist_high_bps`, `dist_low_bps` and the `return_since_*` anchors), which move every minute.
- When the verdict is to enter, the same branch is checked again at the next quote (C + 10 s), and the order goes to the book there.
- Funding: a settlement at time S is known from the first close of a bar of the feature's `tf` at or after S + 60 s. A settlement at 08:00 is known from 08:01 on `1m` and from 09:00 on `1h`.
- Index and mark prices are read one minute late. At C, the newest index or mark minute used is the one that closed at C - 60 s, paired with the swap minute of the same time. On a `tf` of `5m` or longer, when C falls exactly on a bar boundary of that `tf`, the lagged bar would be one bar older than the newest swap bar, so premium and basis features on that `tf` are UNKNOWN at that one decision per bar.
- Lookback must fit in 90 days. Lookback is (n + 1) × tf for price features; (n + 1) × tf + 1 minute for premium and basis; n × 8 h + tf for funding; 7 days + 1 minute for the week anchor, and 1 day + 1 minute for the other anchors. This caps `n` on `4h` and `1d` (section 7).
- Warm-up: the evaluator loads up to 90 days of history before the first decision. The engines load the history a recipe needs when a run starts; until it is there, every entry is UNKNOWN.
- Data holes: exactly one missing 1-minute candle is filled flat (open = high = low = close = the previous close, no volume), and no decision is made on it. A longer hole stays missing.

## 7. Feature catalogue

Values are in bps unless noted. "The last n bars" means the n most recent closed bars of `tf`; the newest of them must be the latest closed `tf` bar, because a feature never falls back to an older bar. Unless stated otherwise, every bar in the window must be complete and the bars consecutive. Reports name a feature `name@tf:n`, for example `return_bps@1h:1`.

### Price features (swap candles, any `tf`)

| Name | n | Value |
|---|---|---|
| `return_bps` | 1-1,440; on `4h` at most 539; on `1d` at most 89 | 1e4 × (close of the latest closed bar / close of the bar n bars earlier - 1). Only these two bars must be complete (in the evaluator; see section 10). |
| `rms_bar_bps` | 2-500; on `1d` at most 89 | √(mean over the last n bars of (1e4 × (close / open - 1))²): the typical move inside one bar, not centred |
| `realized_vol_bps` | 2-720; on `4h` at most 539; on `1d` at most 89 | sample standard deviation (ddof 1) of the n log returns 1e4 × ln(close_j / close_j-1) over the last n + 1 bars |
| `sma_gap_bps` | 1-200; on `1d` at most 89 | 1e4 × (price / mean close of the last n bars - 1); on `1d` with n = 1, price against yesterday's close |
| `dist_high_bps` | 1-200; on `1d` at most 89 | 1e4 × (price / highest high of the last n bars - 1); positive only when price is above that range, so never on `1m`, whose window includes the bar ending at C |
| `dist_low_bps` | 1-200; on `1d` at most 89 | 1e4 × (price / lowest low of the last n bars - 1) |

### Positioning features (any `tf`)

| Name | n | Value |
|---|---|---|
| `funding_settled_bps` | 1-90 | mean of the last n settled funding rates known at the close of the latest `tf` bar, × 1e4, in bps per 8 h; positive means longs pay shorts |
| `premium_bps` | 1-200; on `1d` at most 88 | mean premium of the last n lagged bars; a bar's premium is the mean over its minutes of 1e4 × (swap close / index close - 1) |
| `premium_change_bps` | 1-200; on `1d` at most 88 | premium of the latest lagged bar minus premium of the lagged bar n bars earlier; only these two bars must be complete (in the evaluator; see section 10) |
| `basis_mark_bps` | 1-200; on `1d` at most 88 | as `premium_bps`, with the mark close in place of the swap close |

- A premium or basis minute is valid only when the swap traded in it (volume > 0) and the index close (for basis also the mark close) exists. A lagged bar needs every one of its minutes valid.
- `funding_settled_bps` exactly: let K be the close of the latest closed `tf` bar at C. A settlement S is known at K when S + 60 s ≤ K. Take the known settlements newest first, S1, S2, ..., with step(Si) = Si - Si+1. The value is UNKNOWN when (a) fewer than n + 2 settlements are known; (b) K ≥ S1 + step(S1) + 60 s, that is, the next settlement is overdue; or (c) any of S1 ... Sn has no rate, a step ≤ 0 or > 8 h + 1 min, or a step that differs from the step before it by more than 1 minute. Otherwise it is 1e4 × the mean of the n rates, each rate multiplied by 8 h / step when its step is not within 1 minute of 8 h. So a new settlement interval counts only after two equal steps.

### Anchors (fixed `tf` and `n`; here `n` is a parameter, not a window)

| Name | tf | n | Value at C |
|---|---|---|---|
| `utc_minute_of_day` | `1m` | 1 | minute of the UTC day of C, 0-1,439 (the decision on the bar 13:59-14:00 sees 840); never UNKNOWN |
| `utc_weekday` | `1d` | 1 | UTC weekday of C, 0 = Monday ... 6 = Sunday; never UNKNOWN |
| `us_session_minute` | `1m` | 1 | minutes since 09:30 New York time when C is Monday to Friday with 09:30 ≤ C < 16:00 local time (0-389), otherwise -1; US daylight-saving rules since 2007, holidays ignored; never UNKNOWN |
| `return_since_utc_day_open_bps` | `1d` | 1 | 1e4 × (price / open of the 00:00 UTC minute of the current day - 1); the current day is the day of the newest closed minute |
| `return_since_utc_week_open_bps` | `1d` | 7 | the same, from Monday 00:00 UTC of the current week |
| `return_since_utc_hour_bps` | `1h` | 0-23 | the same, from the hh:00 UTC minute with hh = n, at the latest such hour start (today if it has already been reached, otherwise yesterday) |

### Not in schema 4

Other indicators (RSI, ATR, EMA, volume, range position), cross-asset inputs (there is no `asset` key), feature names given as plain strings (as in the older formats), exit predicates, trailing or ATR stops, setup and confirm stages, risk-based sizing, adds, partial exits and leverage.

## 8. UNKNOWN values and data coverage

A feature is UNKNOWN at C when:

- its history is shorter than its lookback (the start of the data, or an engine run before its warm-up is complete);
- a bar it needs is incomplete or missing, including the latest closed `tf` bar itself (there is no fallback to an older bar);
- for positioning features: an index or mark minute is missing, the swap did not trade in a minute, the funding schedule is broken, unconfirmed or overdue (section 7), or the series is absent;
- the start minute of a return anchor is missing;
- in the engines: the feature snapshot is missing, not yet visible, or a whole bar behind the clock.

UNKNOWN anywhere in a predicate means no entry at that decision, and nothing more. It never closes or changes an open position: stop, target and max hold always run. An UNKNOWN post-only offset feature places no order.

A practical consequence: a window feature needs every bar in its window complete. `rms_bar_bps` on `1h` with n = 168, for example, stays UNKNOWN for 168 hours after any hour that contains a hole of two or more minutes. The evaluator reports the share of time with an UNKNOWN entry rule per quarter.

### Data coverage in the team's store

These facts describe the OKX data the team used for the development period DEV (2020-01-01 to 2026-10-02 18:31 UTC). A store you build yourself can differ; see [QUICKSTART.md](QUICKSTART.md).

- Swap 1-minute candles: BTC and ETH from 2020-01-01, SOL from 2021-01-22.
- Index and mark 1-minute candles: from 2020-01-02 16:00 UTC (SOL from 2021-01-22). The evaluator reads them from stores next to the swap store, named `<BASE>-USDT_index` and `<instrument>_mark`. Without them, premium and basis features are always UNKNOWN and a recipe that uses them never enters.
- Swap minutes without trades (thousands for BTC in 2020, fewer until 2023) void the premium and basis bars that contain them.
- Settled funding: OKX's public API returns only about the last three months, so the team added OKX's monthly funding archive. With it there is no funding before 2021-08-31 16:00 UTC; settlements every 8 h at 00, 08 and 16 UTC from then to 2021-09-30 08:00 and from 2022-01-16 16:00 on; missing slots at 2022-02-25 08:00 (BTC, ETH) and from 2022-02-22 08:00 to 2022-02-25 08:00 (SOL); irregular times on 2022-12-18; and many ETH and SOL records up to 2024-01-17 stamped 1-24 s after their slot.
- For `funding_settled_bps`, a lone missing slot S gives UNKNOWN from S + 1 minute to S + (n + 2) × 8 h + 1 minute on `1m`; a longer `tf` moves each of these times to the first `tf` bar close at or after it. A record stamped seconds late can also make single decisions just after its slot UNKNOWN (rule (b) of section 7). With funding from the API alone, the feature is UNKNOWN for nearly all of DEV.

## 9. Orders, size, exits and costs

### 9.1 The order block

```json
{"type": "taker"}
{"type": "post_only", "offset": {"bps": 5}, "timeout_seconds": 1800}
{"type": "post_only", "offset": {"mult": 0.5, "of": {"name": "rms_bar_bps", "tf": "1h", "n": 24}}, "timeout_seconds": 1800}
```

`offset.bps` is 1-500; `offset.mult` is 0.01-10, with `of` any feature spec; `timeout_seconds` is 60-7,200 and a multiple of 60. No other keys.

### 9.2 The evaluator's quote model

Inside each minute the price runs open → low → high → close when close ≥ open, otherwise open → high → low → close. The path is piecewise linear, the time on each leg is proportional to its length, and it is sampled every 5 seconds. Bid = that price rounded down to the tick; ask = bid + 1 tick.

### 9.3 Taker entry

The order fills at the recheck quote (C + 10 s): a long at ask × 1.0002, a short at bid × 0.9998 (2 bps of slippage), with a fee of 5 bps of quantity × fill price.

### 9.4 Post-only entry

1. The limit L comes from P0, the close of the 1-minute bar that ends at C, and the offset o in bps (`bps`, or `mult` × the value of `of` at C). Long: L = P0 × (1 - o / 1e4), rounded down to the tick. Short: L = P0 × (1 + o / 1e4), rounded up. If `of` is UNKNOWN or o ≤ 0, no order is placed.
2. If the quantity at L would be below one lot, no order is placed (section 9.5).
3. The order is placed at the recheck quote (C + 10 s). It is rejected without a trade if it would cross the book there (long: ask ≤ L; short: bid ≥ L).
4. It fills in full at L at the first later quote within `timeout_seconds` where the price is beyond L by d = max(1 bps of L, 2 ticks): long when the price is ≤ L - d, short when it is ≥ L + d. A touch is not a fill, and there are no partial fills.
5. Costs: a maker fee of 2 bps plus 2 bps of adverse selection, both of quantity × L.
6. Unfilled at the timeout: cancelled, with no trade and no cooldown. While the order rests, no decision is made; the next one is on the first bar visible at or after placement + timeout. After a rejected or skipped order, the next decision is on the next new bar.
7. The gate verdicts use this run (d with 1 bps, adverse selection 2 bps). Reports add a grid (pass-through 1 or 2 bps × adverse selection 2 or 4 bps) and a floor that books the fills of the 2 bps run as taker fills at L × 1.0002 (long) or L × 0.9998 (short) with the 5 bps fee. The grid and the floor are reported, not gated.

### 9.5 Position and size

- One position at a time, one entry per position, no adds or partial exits. With `side: "both"`, a reversal needs a full exit and the cooldown first.
- The evaluator account is 100 USDT and does not compound, so the budget of every entry is `size_pct` USDT. Quantity = floor(`size_pct` / (fill price × lot)) × lot, at the actual fill price: ask × 1.0002 or bid × 0.9998 for a taker entry, L for a post-only entry (checked before the order is placed).
- Below one lot the entry is skipped: no order, HOLD, and the rule is evaluated again on the next bar. The traded notional is a whole number of lots: more than half of `size_pct` USDT and at most all of it.
- For BTC this matters. One lot is 0.0001 BTC, so at 120,000 USDT per BTC it costs about 12 USDT, and a `size_pct` of 12 or less can skip every entry at that price. ETH and SOL lots are cheap enough that every allowed `size_pct` bought at least one lot over the whole DEV.

### 9.6 Exits

- Triggers compare the executable exit price X with the entry fill price: change = s × (X / entry - 1) × 1e4, with s = +1 for a long and -1 for a short, and X = bid × 0.9998 for a long or ask × 1.0002 for a short. They are checked at every 5-second quote. Fees and funding are not part of the trigger.
  - stop: change ≤ -`stop_loss_bps`, live from the fill;
  - target: change ≥ `take_profit_bps`, live from the minute after the fill minute;
  - max hold: position age ≥ `max_hold_seconds`.
- A trigger closes the whole position at the next quote, at that quote's X. Gaps pass through: there is no price guarantee. The exit fee is 5 bps; exits are always taker.
- Candle rule: if the target triggers in a minute whose low (long) or high (short) would also have reached the stop, the trade exits at the stop level, entry × (1 - s × `stop_loss_bps` / 1e4), instead. One-minute candles hide the order of prices inside the minute, so the evaluator assumes the worse case.
- There is no formal exit: no exit predicate, trailing stop or break-even rule. Outside the recipe, the platform can still close a position: an operator stop or account risk limits in the engines, and in the evaluator the end of the data (`forced_close_end_of_period`) or a data hole (`data_gap_exit`), at the last available price.
- `cooldown_seconds` counts from the exit: the next decision is on the first bar visible at or after exit + cooldown (0 means the next bar).

### 9.7 Costs in the evaluator

| Item | Exactly |
|---|---|
| Spread | the quote model above: ask - bid = 1 tick, no other spread. In bps over DEV: BTC at most 0.3, ETH at most 1.2, SOL 0.3-33 (one 0.01 USDT tick at prices from 295 down to 3 USDT per SOL) |
| Taker entry | a long fills at ask × 1.0002, a short at bid × 0.9998 (2 bps of slippage); fee 5 bps of quantity × fill price |
| Post-only entry | fills at L; maker fee 2 bps plus adverse selection 2 bps, of quantity × L |
| Exit (always taker) | a long at bid × 0.9998, a short at ask × 1.0002 (the stop level under the candle rule; the last available price at the end of the data or a hole); fee 5 bps of quantity × exit price |
| Funding with a record | for each settlement S with fill < S ≤ exit, the position pays s × quantity × rate × mid, where mid = (bid + ask) / 2 of the last quote before S; a long pays a positive rate and a short receives it |

- Missing funding (the conservative scenario). For every 00:00, 08:00 and 16:00 UTC slot t with entry < t ≤ exit that lies before the first funding record or in a run of missing slots, the trade pays its entry quantity × entry fill price × r, long and short alike. r = max(mean absolute funding rate from 2026-06-29 00:00 to 2026-10-02 18:32 UTC, 0.0001); in the team's data this was the floor, 1 bps per slot, for BTC, ETH and SOL. A slot counts as missing when it lies between the first and the last record and no record is within 3 hours of it; in the team's data, from 2021-09-30 16:00 to 2022-01-16 08:00 (324 slots) for all three, plus 2022-02-25 08:00 (BTC, ETH) or 2022-02-22 08:00 to 2022-02-25 08:00 (SOL, 10 slots). The gate verdict uses this scenario. The base scenario (no charge) is reported beside it, and so is a sensitivity scenario at 0.0003 per slot when the mean hold is longer than 8 hours. The charge changes only the result, never a feature value.
- Stress. After the run, each trade loses another 5 bps of its executed notional (entry notional plus exit notional), on top of the conservative scenario. Decisions and fills are not simulated again. Every stress check of the gates uses this number.
- So a taker round trip costs 14 bps of notional plus 1 tick (24 bps plus 1 tick under stress), and a post-only entry with a taker exit costs 11 bps (21 bps under stress), before funding. Reports also show the post-only grid and floor (9.4) and a stop-slippage stress of max(2 bps, 0.25 × the high-low range of the trigger minute) of each stop exit's notional; these are not gates.

## 10. Engines versus the evaluator

The evaluator is the reference for every gate. The engines (workshop replays, PAPER, Demo and Live) read the same recipe with the same feature code and timing rules, but they work on actual quote streams (live, recorded or synthetic) instead of the modelled path, so some details differ:

- An entry is skipped when the spread is above 2 bps, a fixed engine setting.
- Taker entries on Demo and Live are immediate-or-cancel limit orders priced 2 bps through the best price (rounded to the tick), never unbounded market orders. PAPER simulates the fill.
- A post-only order on Demo and Live is a real resting order. It rests until it fills or the recipe's timeout passes, then the rest is cancelled; a partial fill is kept and protected. PAPER simulates the fill with the evaluator's pass-through rule on the last traded price.
- Exits on Live compare the best bid (long) or best ask (short) without the evaluator's extra 2 bps, and they are checked once per engine step rather than at every 5-second model quote; max hold fires at the first step after its deadline. An exit is a reduce-only market order. After each fill the engine also places a stop order hosted by the exchange (triggered by the last price, executed as a market order), which can fire between steps.
- The candle rule of 9.6 exists only in the evaluator; the engines see the real order of prices.
- Around data holes, `return_bps` and `premium_change_bps` are stricter in the engines. The evaluator needs only the two end bars complete. The engines' feature book, like the reference functions in `strategy_v4.py`, also gives UNKNOWN when a bar between the two has no minute at all, for example after a hole of two or more minutes on `1m`.
- Some limits are not part of the recipe: a cap on entries per round, an account drawdown stop, the operator's Stop button, and the leverage setting. In the owner's Live rounds, a schema-4 bot traded at most one minimum lot whatever its `size_pct`.

## 11. Validation errors

### 11.1 Validator codes

The validator stops at the first failure and reports one code. One exception: a list or an object where `op`, a feature `name` or a `tf` should be raises a Python `TypeError` instead; the workshop then prints its generic message, and `team_eval.py` a traceback.

| Code | Cause |
|---|---|
| `v4_payload_keys` | the payload is not an object, its keys are not exactly the eight top-level keys, or `strategy_schema_version` is not 4 |
| `v4_instrument` | unknown instrument |
| `v4_text_fields` | `name` is not a string of 1-200 characters, or `description` or `original_text` is not a string of 1-20,000 |
| `v4_author_or_confirmation` | `confirmed` is not `true`, or `author` is not an object |
| `v4_rule_keys` | `rules` is not an object with exactly the eight rule keys |
| `invalid_number` | a value that is not a finite number (booleans included), is out of bounds, or is fractional where an integer is required: the rule numbers, `n`, `value`, `mult`, offsets, the timeout |
| `hold_and_cooldown_whole_minutes` | `max_hold_seconds` or `cooldown_seconds` is not a multiple of 60 |
| `v4_side` | `side` is not `"long"`, `"short"` or `"both"` |
| `v4_entry_shape` | `entry` is not `{"when": P}` (long, short), or not `{"long": {"when": P}, "short": {"when": P}}` (both) |
| `predicate_depth_nodes_or_shape` | a node that is not an object, depth above 6, or more than 64 nodes |
| `predicate_group_size` | an `all` or `any` value that is not a list of 1-8 nodes |
| `predicate_operator` | a node that is not `all`, `any` or `not` and has no valid `op` |
| `predicate_leaf_shape` | leaf keys that are not exactly `feature, op, value` or `feature, op, ref, mult` |
| `predicate_zero_multiplier` | `mult` = 0 |
| `feature_shape_name_or_timeframe` | feature spec keys that are not exactly `name, tf, n`, or an unknown name or timeframe |
| `anchor_timeframe` | an anchor on a timeframe other than its fixed one |
| `feature_lookback_over_90_days` | lookback over 90 days, for example a `1d` price feature with n ≥ 90 or `return_bps` on `4h` with n ≥ 540 |
| `take_profit_below_published_cost_model` | `take_profit_bps` ≤ 14; cannot happen with the floor of 40 |
| `order_shape` | `order` is neither `{"type": "taker"}` nor a `post_only` object with exactly `type`, `offset` and `timeout_seconds` |
| `order_offset_shape` | `offset` is neither `{"bps": ...}` nor `{"mult": ..., "of": ...}` |
| `order_timeout_whole_minutes` | `timeout_seconds` is not a multiple of 60 |

`validate` and `lint` are this validator, and gate K's form check is the same. The evaluator refuses any recipe the validator rejects.

### 11.2 Engine admission

The web workshop, the arena and the exchange engines run more checks before the validator. A recipe that fails only these stays valid for the evaluator. Their messages are in Ukrainian:

| Message | Meaning |
|---|---|
| `Підтвердь відповідність формальних правил оригінальному опису.` | `confirmed` is not `true` |
| `Заповни паспорт автора.` | `author` does not have exactly the seven passport keys |
| `Невідомий тип автора.` | `kind` is not `human`, `model` or `example` |
| `Текст має містити від 1 до 60 символів.` (the numbers vary) | a text is not a string, is too long, or is empty where it is required |
| `Недопустимі керувальні символи.` | a control character below U+0020 other than newline, tab or carriage return |
| `Очікується звичайне число.`, `Число має бути в межах 1…1000.`, `Кількість спроб має бути цілою.` | `attempts` is not a number, is out of range, or is not whole |
| `Для стратегії моделі потрібна дата отримання відповіді.` | `kind` is `"model"` and `submitted_at` is empty |
| `Паспорт автора схеми 4 має бути вже чистим: без зайвих пробілів, кількість спроб ціла.` | a passport text has leading or trailing whitespace |
| `Назва й описи схеми 4 мають бути вже чистими: без пробілів на краях, у межах довжини.` | `name`, `description` or `original_text` has leading or trailing whitespace |
| `Схема 4: <code>` | the validator rejected the recipe; `<code>` is from 11.1 |

The engines also require an executed instrument and `take_profit_bps` > 16. Neither check can fail for a recipe that passes the validator.

## 12. Validator output and the recipe hash

On success the validator returns the cleaned recipe (`n`, `timeout_seconds` and `strategy_schema_version` as integers) plus six derived fields that a recipe file must not contain: `semantics`, `recipe_sha256`, `eval_only`, `catalogue`, `execution_model_id` and `semantic_sha256`.

- `recipe_sha256` is the SHA-256 of the cleaned recipe plus `semantics`, as canonical JSON: sorted keys, separators `,` and `:`, UTF-8, non-ASCII characters kept as they are. It covers every field, the passport and all texts included. Indentation, line breaks and key order in the file do not matter. Numbers count as written: `3` and `3.0` give different hashes (except `n`, `timeout_seconds` and `strategy_schema_version`, which become integers).
- A frozen recipe is identified by this hash. Evaluator ledgers record it, and the strategy cards give the expected value for the recipes they describe. Before you reproduce a published result, check the hash.
- `semantic_sha256` covers only the semantics, catalogue and execution model labels, the instrument and the rules. Two recipes with the same rules and different texts share it.
- `eval_only` is always `true` in this output. It is a historical flag and does not tell you whether the engines admit the recipe.
- Inside workshop and arena records, the engines keep their own fingerprint of the payload under `recipe_sha256` and the validator's hash under `v4_recipe_sha256`. The hash in this repository's documents and evaluator ledgers is the validator's.
- After a validation error, change only what the error reports. Any other change, even to a text, makes a different recipe.

To recompute the hash yourself:

```python
import hashlib, json
from trading_lab.strategy_v4 import validate_payload

clean = validate_payload(json.load(open("recipe.json", encoding="utf-8-sig")))
core = {key: clean[key] for key in ("strategy_schema_version", "name", "author", "description",
                                    "original_text", "confirmed", "instrument", "rules", "semantics")}
text = json.dumps(core, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
assert hashlib.sha256(text.encode("utf-8")).hexdigest() == clean["recipe_sha256"]
```

## 13. Example 2: H10-ETH, field by field

H10-ETH was written by "Sonnet", the made-up name of an AI author session running on Claude Opus 5.5, and frozen on 2026-10-28. It is the only recipe of the project that passed gate K, with a thin margin. Its forward test (gate D) stopped at 1 of the 30 trades it needed, so the verdict stayed INSUFFICIENT. It is not proven, and nothing here calls it profitable. The full results and caveats are on its card, [strategies/sonnet-h10-eth/](../strategies/sonnet-h10-eth/).

The file is [`strategies/sonnet-h10-eth/recipe.json`](../strategies/sonnet-h10-eth/recipe.json); a byte-identical copy is at `team/authors/sonnet/candidates/H10_ETH/recipe.json`. Check it:

```
py -3.12 -X utf8 -B tools/strategy_workshop.py lint strategies/sonnet-h10-eth/recipe.json
```

The output says `"valid": true`, `"recipe_sha256": "dcf486662716f1b7a986c0e9f2f0d50d93d6fba8ad928d81f73541e77f906a81"`, `"engines": "workshop (synthetic), Demo and Live (RULES_ONLY)"` and `"features": ["return_bps@1m:60", "rms_bar_bps@1h:168", "sma_gap_bps@1d:20"]`.

### Top level

The texts are in Ukrainian. They stay as they are: the hash covers them, and translating them would make a different recipe.

| Field | Value | Notes |
|---|---|---|
| `strategy_schema_version` | `4` | |
| `name` | `Sonnet H10 · ETH: сплеск угору в тренді (post-only)` | "upward burst in a trend"; 51 characters, within the engine limit of 60 |
| `author.name` | `Sonnet` | a persona name, not a model name |
| `author.kind` | `model` | |
| `author.model` | `claude-opus-5-5` | declared; no tool verifies it |
| `author.submitted_at` | `2026-10-28` | a date without a time; the format is not checked |
| `author.prompt` | one line in Ukrainian | names the author's role in the team and its internal research notes |
| `author.attempts` | `1` | |
| `author.edited_by` | `""` | nobody edited the recipe |
| `description` | 261 characters | see below |
| `original_text` | 1,324 characters | see below |
| `confirmed` | `true` | |
| `instrument` | `ETH-USDT-SWAP` | |

`description`, in English: ETH-USDT-SWAP, long only, 1 lot. After a sharp 60-minute rise (at least 3 typical hourly moves, measured over 7 days) with the price above its 20-day average, place a post-only limit buy a quarter of a typical hourly move lower and keep it for 2 hours. Hold for up to 24 hours, with a 5 % stop and a 10 % target. The "1 lot" is not a recipe field: in the evaluator the size follows `size_pct` (section 9.5), and in the owner's Live rounds the engine capped schema-4 bots at one minimum lot.

`original_text`, summarised in English: the hypothesis (an upward burst in an uptrend tends to continue for about a day, driven by fear of missing out, short covering and late trend buyers; falls did not behave this way, so the recipe is long only); the three features and the rules, in words; how the configuration was chosen (on BTC, among 4 maker-entry variants, then moved to ETH unchanged, replicated blind on ETH for 2020-01 to 2025-06, and only then put through the full gate K with one look at the last five quarters); the weaknesses the author saw (only those last quarters were out of sample, t ≈ 2.3; the profit rests on rare large winners; about 0.2 trades a day; the edge depends on ETH's high volatility relative to costs, and the same configuration failed on BTC when BTC's volatility fell; the maker entry still needed Live calibration); that another author had found the same effect on BTC independently, and that author's materials were not used; and that none of this is evidence of future profitability.

### Rules, verbatim

```json
"rules": {
  "side": "long",
  "entry": {
    "when": {
      "all": [
        {
          "feature": {"name": "return_bps", "tf": "1m", "n": 60},
          "op": "gte",
          "ref": {"name": "rms_bar_bps", "tf": "1h", "n": 168},
          "mult": 3.0
        },
        {
          "feature": {"name": "sma_gap_bps", "tf": "1d", "n": 20},
          "op": "gt",
          "value": 0
        }
      ]
    }
  },
  "order": {
    "type": "post_only",
    "offset": {"mult": 0.25, "of": {"name": "rms_bar_bps", "tf": "1h", "n": 168}},
    "timeout_seconds": 7200
  },
  "stop_loss_bps": 500,
  "take_profit_bps": 1000,
  "max_hold_seconds": 86400,
  "cooldown_seconds": 600,
  "size_pct": 11
}
```

- `side: "long"`: buys only. The recipe never shorts.
- `entry.when.all`: both leaves must be TRUE. If either is UNKNOWN, there is no entry.
- First leaf, `return_bps@1m:60 ≥ 3.0 × rms_bar_bps@1h:168`:
  - `return_bps` on `1m` with n = 60 is the move from the close 60 minutes earlier to the close of the newest closed minute. In the evaluator only those two 1-minute bars must be complete; the engines also need every minute between them (section 10). It changes every minute.
  - `rms_bar_bps` on `1h` with n = 168 is the root mean square of the open-to-close move inside each of the last 168 closed hours (7 days): a typical hourly move. All 168 hourly bars must be complete. It changes once an hour.
  - With `ref` and `mult: 3.0`, the leaf is TRUE when the rise over the last 60 minutes is at least three typical hourly moves.
- Second leaf, `sma_gap_bps@1d:20 > 0`: the price (the newest 1-minute close) is above the mean close of the last 20 closed UTC days. Because it uses the price, it moves every minute. All 20 daily bars must be complete.
- `order`: a post-only limit buy. The offset is 0.25 × `rms_bar_bps@1h:168` (a quarter of a typical hourly move) below P0, the close of the decision's 1-minute bar. The order rests for up to 7,200 s (2 hours), and no decision is made while it rests. Unfilled, it is cancelled with no trade, and the rule is evaluated again on the first bar visible at or after the timeout.
- `stop_loss_bps: 500`: close at a 5 % loss, measured on the executable price, live from the fill.
- `take_profit_bps: 1000`: close at a 10 % gain, live from the minute after the fill.
- `max_hold_seconds: 86400`: otherwise close 24 hours after the fill. On the whole DEV run, 431 of the 550 trades ended this way, 85 at the stop and 34 at the target.
- `cooldown_seconds: 600`: the next decision comes 10 minutes after an exit at the earliest.
- `size_pct: 11`: a budget of 11 USDT per entry on the 100 USDT evaluator account. An ETH lot is 0.001 ETH, so at every ETH price in DEV the budget bought at least one lot.

### A walk-through with made-up numbers

Suppose, for illustration only, that the typical hourly move `rms_bar_bps@1h:168` is 60 bps and the decision bar closes at P0 = 3,000.00 USDT.

- The entry needs a rise of at least 180 bps (1.8 %) over the last 60 minutes, with the price above its 20-day average.
- The offset is 0.25 × 60 = 15 bps, so L = 3,000.00 × (1 - 0.0015) = 2,995.50, already on the 0.01 tick.
- The fill threshold is d = max(1 bps of L, 2 ticks) = max(0.30, 0.02) ≈ 0.30 USDT. The order fills only if the model price reaches about 2,995.20 or lower within 2 hours of placement.
- The quantity is floor(11 / (2,995.50 × 0.001)) = 3 lots = 0.003 ETH, about 8.99 USDT of notional. The entry costs 4 bps of that: 2 bps maker fee and 2 bps adverse selection.
- The stop fires when bid × 0.9998 ≤ 2,995.50 × 0.95, which is a bid of about 2,846.29 or lower. The target fires when bid × 0.9998 ≥ 2,995.50 × 1.10, a bid of about 3,295.71 or higher, but only from the minute after the fill. Otherwise the position closes 24 hours after the fill, plus one 5-second quote.

### What it needs from the data

The longest lookback is `sma_gap_bps@1d:20`: 21 days of history before the first possible entry. A hole of two or more minutes makes its hour and its day incomplete, which blocks entries for 168 hours (through `rms_bar_bps`) and 20 days (through `sma_gap_bps`). The team's ETH store had no such hole in DEV.

### How it did

Gate K: PASS on 2026-10-29, with a thin margin (whole DEV: 550 trades, net +30.81 USDT, +25.33 under stress, on a 100 USDT account; 7 of 11 active quarters positive in E2, against the 60 % required). Gate D: INSUFFICIENT, with 1 of the 30 forward trades it needed. Not proven. See [the card](../strategies/sonnet-h10-eth/) for every number and how to reproduce it.

## 14. Checklist for a new recipe

1. Write `original_text` first, in plain words, including the weaknesses you already see. Formalise it in `rules`, and only then set `confirmed: true`.
2. Fill in the seven-key passport, even for local tests: the web workshop and the arena need it.
3. Check the lookbacks. The longest one is your warm-up, and every window must be complete, so a data hole blocks entries for the length of the window.
4. Check `size_pct` against the lot price. For BTC, the budget must cover 0.0001 BTC × the highest price you expect × 1.0002.
5. Compare your target and the moves you hope to catch with the round-trip cost: 14 bps plus the spread with taker entries, 11 bps with post-only entries, plus funding on long holds. Most of this project's fast ideas failed at exactly this point; see [RESULTS.md](RESULTS.md).
6. Run `validate` and `lint`. Then write down the test and the number of variants you will try before you run the evaluator ([METHODOLOGY.md](METHODOLOGY.md), section 8).
7. Freeze the recipe once it is evaluated. Any later change, a typo included, makes a new recipe with a new hash that needs its own test.
