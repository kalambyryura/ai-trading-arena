# Channel entries

Each role writes only its own file, `team/channel/<role>.md`, and only appends at the end. A correction is a new entry that names the one it corrects. Never edit or delete an old entry.

## Header

```
### <YYYY-MM-DD HH:MM> <TZ> · <from> → <to>[, <to2>] · <TYPE>
```

- `<from>` and `<to>` are role names; `all` addresses everyone. Relayed: `alpha → coord → judge`, or `judge → platform (via coord)`.
- 1–8 lines of text follow. Decisions may be longer.
- The time comes from the clock, in the same command that writes the entry:

```bash
T=$(date '+%Y-%m-%d %H:%M %:z')
printf '\n### %s · alpha → coord · RESULT\n%s\n' "$T" "Cycle C1 closed: no candidate. Report: team/authors/alpha/C1/RESULTS.md (commit abc1234)." >> team/channel/alpha.md
```

## Types

| Type | Use | Example first line |
|---|---|---|
| INFO | news, plan, digest | `Day 2 plan: PREREG P003 today, runs tomorrow after the heavy-run slot.` |
| ASK | a question | `Does the evaluator charge funding on open positions at the end of data?` |
| ANSWER | a reply, naming the ASK | `Re 2025-03-14 10:02 ASK: yes, at the last price with taker costs (tools/eval.py:812).` |
| CLAIM | reserve a task or file until a time | `CLAIM data download BTC 2020–2025 until 18:00. Checked BOARD and channels: nobody on it.` |
| DONE | finished work with the commit | `PREREG P007 and its TRIALS line committed (abc1234). Runs start now.` |
| BLOCKED | needs someone else, or the guard refused | `Guard blocked [path.write-deny]: <exact command>. Needed because <why>. Working on P008 meanwhile.` |
| REQUEST | a feature for the platform | `REQUEST: trailing stop in the recipe format, for long-horizon tests (needed by P009).` |
| RESULT | a research outcome with the report path | `C1: 24 configs, 0 qualified; tail look −0.8 on 11 trades. No candidate.` |
| PROPOSAL | for the judge or the lead | `PROPOSAL: adapted gate for rare-trade strategies, date-shift test; details in <path>.` |
| ACK | acknowledging a decision | `ACK D057: brief and STATUS updated; budget 24 configs noted.` |
| DECISION | lead only | `D057 — new research cycle C1. Owner: "Yes, start it" (lead's chat, 2025-03-14 15:59).` |

## A decision entry (lead only)

```
### 2025-03-14 15:59 +02:00 · lead → all · DECISION
D057 — new research cycle C1. Owner: "Yes, start it" (lead's chat, 2025-03-14 15:59).

**Goal.** …
**Principles, mandatory for every candidate:** 1. … 2. …
**Directions.** alpha — …; beta — …
**Deadlines.** day 1 — PREREG and grid; days 2–3 — runs; day 4 — RESULTS.
Basis: team/reviews/…, commit abc1234.

**Next:** …
```
