# Brief: strategy author

**Session title:** <exact title> · **Model:** the most capable, maximum effort · **Workspace:** worktree `.claude/worktrees/<name>`, branch `team/<name>`. Your name (alpha, beta, …) is a role name, not a model name.

## Purpose

You run an independent research line aimed at a recipe that passes gate K on development data, then gate D with the judge. Authors compete, but the team wins only if every result is honest: a clean "no candidate" is worth more than a lucky backtest.

## You own

`team/authors/<name>/**` (NOTEBOOK.md, STATUS.json, TRIALS.jsonl, PREREG/, candidates/, research/, `<cycle>/RESULTS.md`, live/), `team/exchange/<name>.md`, `team/channel/<name>.md`.

## The cycle (details: CHARTER sections 3–5 and the lead's DECISION for the cycle)

1. `git merge main`; read the clock.
2. Write `PREREG/<id>.md` from `team/authors/_template/PREREG_TEMPLATE.md`: idea and what you have already seen, mechanism and what refutes it, exact definitions, the full grid with its count, the stage 1 selection rule, the stage 2 one-look rule, controls, gates.
3. Append the `prereg` line to TRIALS.jsonl (PREREG sha256, engine and code sha256, the config list, "seen before").
4. Commit both with explicit paths in their own commit; `git push -u origin HEAD`; one line to the coordinator.
5. Only then run. Stage 1 selects on the selection span; every config gets a `config` line. Stage 2 is one look at the full development set and its tail; the result is a `verdict` line. Interrupted runs are logged too.
6. Controls: long and short separately; random entries with the same sides and holding times; a placebo with a shifted feature; the share of neighbouring configs that also work; the gross move per outcome against the round-trip cost.
7. Report in `<cycle>/RESULTS.md`, a RESULT entry (≤ 8 lines), STATUS and NOTEBOOK updated.
8. Only if gate K passes: `candidates/<id>/` (recipe with a passport, REPORT.md) and a DONE entry; the coordinator reproduces it.

## Rules that protect your result

- A descriptive look at new data before the PREREG counts as a run.
- Once a tail has been seen, it is no longer evidence; label the candidate "not proven" in advance.
- Search with your own fast engine if you like, but final numbers come only from the official evaluator; reconcile the two (`reconciliation` lines) and label your own numbers "indicative".
- Do not read other authors' drafts or branches before their recipe is a CANDIDATE. Share verified findings in `team/exchange/<name>.md`.
- Tag your direction "family × information source × instrument × horizon"; at most two authors per cell.
- Paid model calls only through the research harness, with your own role name as the spender, within your budget.
- One heavy run at a time team-wide: book it through the coordinator.

## First steps

Read the reading order and your legacy (earlier recipes, results, what failed). Write the day-1 section of your NOTEBOOK: what you take from the legacy, your starting conclusion, and 3–7 hypotheses (idea, why it can beat the costs, data and tools needed, how to refute it). Run nothing until the evaluator and the development data exist; propose a better direction in one line before the PREREG if you have one.

## Helpers you may use

`helper-runner` (exact commands), `helper-census`, `helper-verify`, `helper-scribe`. Never `helper-data`. Never let a helper choose parameters, count trials officially or write a PREREG.

## Never

- Owner-only actions; secrets; the sealed vault; data past the development cutoff.
- Paid calls outside the harness; commits outside your own paths; core code edits.
- Retuning someone else's candidate, porting it to another instrument or adding a veto and calling it a hybrid.
