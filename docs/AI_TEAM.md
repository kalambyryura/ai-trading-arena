# How a team of AI agents ran this research

From 2026-10-28 to 2026-11-17 this project was run by one human owner (GitHub: kalambyrYura) and a team of Claude Code sessions with fixed roles. This page explains who did what, which rules they worked under, the numbers, and what worked and what did not.

Everything here comes from the team's own working files: the charter, the protocol, the role briefs, the decision channel, the release notes, the review files and the round register. Those files were written in Ukrainian. Commit counts refer to the private working repository; this public repository starts without that history.

## In short

- Eleven long-running AI sessions with fixed roles, plus five small helper agents, worked in one Git repository on one laptop. A second Windows PC, running around the clock, hosted the trading panel and the Live rounds.
- The owner kept every action that moves money or starts trading: Start, Stop and Resume of rounds, exchange keys and funds. Release installs on the trading PC were later delegated case by case, each time with the owner's own written consent.
- The written process caught real problems. Independent reviews stopped several faulty releases before they reached the trading PC, and pre-registration with a trial ledger kept negative results on the record. Supervision of Live rounds by AI sessions was the weak spot.
- The process did not find an edge. No strategy reached the "proven" status. On 2026-11-17 the owner wound trading down and decided to publish the project as open source.

## Before the team

Until 2026-10-28 a Codex agent maintained the repository. Its notes stayed in the repository as historical sources of facts, not as instructions.

On 2026-10-28 the lead session interviewed the owner in three rounds. The answers became twelve owner decisions, G1 to G12, recorded together as decision D093. The ones that shaped the team:

- A Claude lead decides how the work is organised. The repository moves to a private GitHub repository of the owner.
- A separate platform engineer extends the shared tools. Releases are identical for all authors.
- Authors may share findings, but each stays independent and checks every idea itself, including ideas taken from others.
- Paid research calls to Jev, a third-party decision model (`typesafe/jev-1.13`, reached through OpenRouter), are capped at $1.00 per author in total.
- A new strategy version may trade Live with the minimum order size and a "test" label. Full size and the word "profitable" come only after the final check.

## Roles

### Owner (human)

- Made the decisions that only the owner can make: goals, budgets, scope, and whether to go on.
- The only one allowed to press Start, Stop or Resume for any round (Live, Demo or paper). Held the exchange keys and funds. Installed releases on the trading PC, and later gave written consent, case by case, for the operator to do it.
- Also had a strategy slot of their own; see MYHuman below.

### Lead

- Model: Claude Opus 5.5, maximum effort.
- Wrote the charter, the protocol and every role brief. Made the numbered decisions (DECISION entries). Talked to the owner and turned the answers into rules. The only role allowed to change the guard hook.
- Did not commit after the team started; the coordinator committed the lead's files.
- Why: one role had to own the rules and the strategic questions (scope, gates, budgets, questions for the owner, conflicts), so that the others could work without renegotiating them.

### Coordinator

- Model: Claude Sonnet 5.5.
- The only integrator and committer on the trunk branch, and the only role that pushed it to GitHub.
- Before each merge, checked that an author branch touched only that author's paths and passed a secret scan, and that a platform release passed the full test suites and had release notes.
- Reproduced an author's numbers with the shared evaluator and checked for clones before granting "candidate" status.
- Kept the team board and a numbered queue of actions waiting for the owner. Turned the owner's exported round archives into sanitized packets for authors (no keys, no account ids) and logged when each author received one.
- Why: to free the lead for strategy, and to have exactly one place where all branches meet.

### Platform engineer

- Model: Claude Opus 5.5.
- The only role allowed to change the trading core, the shared tools, the tests, the web panel and the deployment kits. Built the shared evaluator, the strategy SDK, the Jev research harness, the Live kits for the trading PC and, later, the notifier.
- The core is protected by hashes, so every core change shipped as a numbered release: full tests, release notes (what changed, effect on the hashes, compatibility) and one identical package for all authors. A feature requested by one author shipped to everyone in the same release.
- Did not write strategies and did not give strategy hints to individual authors. Tested only against fake exchanges and temporary folders.
- Why: identical, honest tools for all four authors, and reliable Live kits for the owner.

### Strategy authors: Astra, Sol, Sonnet, Opus

- Model: Claude Opus 5.5, maximum effort, all four.
- Each worked in its own Git worktree and branch and committed only its own folder, its findings file and its channel file. Each researched freely: improve its first version or replace it with something new, with or without Jev.
- Competition with sharing: verified findings, including negative results, went into a per-author findings file that everyone could read. Drafts stayed private until a recipe reached candidate status.
- Why: competing, independent lines, so that nobody runs after the first idea that looks profitable.

The names are fictional and do not name a model. In the team era all four ran on Claude Opus 5.5. Their first versions (V1) came from earlier chats: the Astra and Sol V1 recipes were written on 2026-09-30 by two chats labelled `gpt-6-astra` and `gpt-6.1-sol` (model names as declared, not verified); the Opus and Sonnet V1 recipes came from Claude Opus and Claude Sonnet sessions on 2026-10-01 and 2026-10-02.

### MYHuman: the owner's strategy slot

- Model: Claude Opus 5.5. Started 2026-10-30.
- An assistant session that turned the owner's own strategy ideas into testable rules and tested them under the same gates, costs and data as the AI authors. The owner made the specification decisions; the session offered options and showed their consequences, but did not tune numbers to history on the owner's behalf.
- Later it combined components proposed by the four AI authors (each wrote a one-page proposal without reading the others) into recipes for the owner's arena slot, shown as MYHuman. None of these recipes passed the candidate gate. The last one was still submitted to the judge, labelled "not proven"; its sample had 0 of 30 trades when trading stopped.
- Why: so the human owner could compete on equal terms.

### Judge

- Model: Claude Opus 5.5. Started 2026-10-30.
- Held data the authors never saw: a sealed period after the end of the development data, and all forward data after 2026-10-28. Ran one evaluation per frozen recipe hash, accepted at most two submissions per author in 14 days, and reported only PASS, FAIL or INSUFFICIENT with aggregate numbers. Before the first verdict it reviewed the shared evaluator for look-ahead and unfair fills. It also decided which Live rounds count toward the proof gate.
- Why: authors should not grade their own work, and hidden data stays hidden only if one role holds it.

### Reviewer

- Model: Claude Opus 5.5; maximum effort for anything in the path of orders, positions, protection, accounting and recovery, slightly lower for documents and UI. Started 2026-11-04.
- Reviewed every platform release candidate before it was merged and installed. Checked each platform plan against the actual code before coding started. Reviewed every Live round that had a fill, an anomaly, a pause, a recovery or an incomplete journal, and the first round on each new release. Quiet rounds got a short receipt from helper agents instead.
- Verdicts were ACCEPT, ACCEPT WITH FINDINGS or REJECT, against a criterion the lead set before the review. Every finding had to be confirmed by a skeptic sub-agent or by a mutant test that fails.
- Never edited code; it proposed patches inside the review and ran probes only on copies of the code. It did not review its own proposals.
- Why: until then the lead ran the reviews itself, through workflows of independent sub-agents, while also writing the decisions for the code. The author of a decision was checking its result, and reviews were filling the lead's context.

### Operator of the second PC (`ops`)

- Model: Claude Opus 5.5, high effort. Started 2026-11-04.
- The only session allowed to drive the remote-desktop tab to the trading PC. Prepared a "Start card" before each round (what was checked, what to select, what to press, what not to touch). Watched rounds read-only at about 35 minutes and during the first two hours. Collected an evidence package after each round and kept the round register.
- With the owner's consent, written by the owner directly in the operator's chat and naming the kit hash and the steps, it could install a release between rounds, import a recipe, run the read-only access check, move files, and fill in the Start form while the owner was present. It never pressed Start, Stop or Resume. Consent passed on by another session did not count.
- Remote desktop was awkward: modifier keys did not reach the remote PC and the frame changed size. The brief required zooming in and reading every button label before a click.
- Why: remote-desktop work was filling the lead's context, and the PC that trades real money was to have a single remote operator.

### Helper agents

- Model: Claude Haiku 5.5, low effort. Five project agents, each defined by a short rule card:
  - `tr-h-runner` runs the exact commands of a job card (tests, validation, evaluator runs) and returns the result lines;
  - `tr-h-census` makes inventories: file lists, counts, trial-ledger summaries, data spans;
  - `tr-h-verify` checks cited claims against files and returns CONFIRMED, REFUTED or UNVERIFIABLE (UNVERIFIABLE when unsure);
  - `tr-h-scribe` formats already verified rows into tables and drafts; it adds no facts;
  - `tr-h-data` fetches public market data and checks the hash manifests (platform, coordinator and judge only).
- They were never given strategy ideas, parameter choices, official trial counts, pre-registrations, verdicts, core changes or owner actions.
- Why: routine work on a cheap model, so the expensive sessions spend effort on judgement.

### An external author slot that stayed empty

On 2026-10-30 the team prepared a slot for an outside AI author (a model from another vendor) that would work only through files carried by the owner. Its replies were to be treated as untrusted data, not instructions. The team board still lists the slot as waiting for its first reply.

## How the work was organised

### Git: one trunk, one integrator

- The trunk is `main`. Every role except the lead and the coordinator worked in its own Git worktree and branch.
- Each role committed only its own paths. Authors could push only their own branch; pushing to `main` from an author worktree was blocked.
- The coordinator merged branches with `--no-ff` after the checks above. After each integration: secret scan, then push of `main`, all branches and tags, then a comparison with the remote.
- `git add` only with explicit paths. No force push, no rebase of pushed branches, no history rewriting. Only the owner could change the remote, the Git configuration or the Git hooks; otherwise an allowed push could be redirected somewhere else.
- Pushes to the owner's private GitHub repository were pre-approved only in the plain forms `git push origin ...` and `git push -u origin ...` (decision D123). The guard hook kept the "no force" rule.

### Communication through files

- Each role had an append-only channel file in Git. Each entry had a header with date, time, sender, receivers and a type: INFO, ASK, ANSWER, CLAIM, DONE, BLOCKED, REQUEST, or DECISION (lead only), followed by one to eight lines.
- A short direct message to another session served as a quick signal and pointed to the channel entry.
- The coordinator kept a team board (state of every role, candidates, the queue for heavy runs, blockers) and the owner's action queue with exact steps.
- Escalation went author or platform, then coordinator, then lead, then owner. Rule changes took the same path: a role asked through the coordinator, the lead answered with a numbered decision and updated the charter.
- Authors sent the coordinator a digest of up to 8 lines a day; the coordinator sent the lead up to 15.
- The reason for files: they are a durable record, and a remote agent could join the team with `git pull`.

### Research rules

The data was split by who may see it:

| Set | Period (UTC) | Who sees it |
|---|---|---|
| DEV | 2020-01-01 to 2026-10-02 18:31 | everyone; used for research and the candidate gate |
| SEALED-1 | 2026-10-02 18:32 to 2026-10-28 00:00 | platform (loading and hashes only) and the judge |
| FORWARD | after 2026-10-28 00:00 | the judge; authors only through sanitized Live packets |
| LIVE | the owner's real rounds | everyone, in sanitized packets |

- **Pre-registration (PREREG).** Before every test series an author wrote down the hypothesis, what exactly is tested, the parameters or grid, and the selection rule, and recorded its hash in the trial ledger before running. In the last cycles the rule was stricter: merge `main`, commit the PREREG and its ledger line, and only then run.
- **Trial ledger (`TRIALS.jsonl`).** Every attempt, including failed and interrupted ones, with a count of the configurations tested. Cycles had caps such as 12, 20 or 24 configurations, because every extra try raises the chance of a lucky result.
- **One look at the tail.** In the later research cycles the last five quarters of DEV (2025-07-01 to 2026-10-02) were held out during selection. The pass criteria were fixed before a single look. If the look failed, the line was closed.
- **Gate K (candidate)** on DEV with the shared evaluator: costs of 5 bps fee plus 2 bps slippage per fill, spread and funding; still positive with 5 bps of extra stress per fill; at least 100 closed trades; checks by market epoch and on the tail; still not negative without the 10 best trades; a PREREG and a complete trial ledger.
- **Gate D (proven)**, run by the judge on sealed and forward data: a frozen recipe hash; a number of trades of at least 20, declared in advance; positive net and stressed results; largest trade at most 30% of gross profit; drawdown at most 10% of the 100 USDT allocated to each bot; and at least two Live rounds of 24 hours or more with fills.
- **Independence.** Hybrids built on another author's candidate needed attribution and a new mechanism. Clones were rejected: more than 50% shared entries within 30 minutes, or a daily PnL correlation above 0.8. At most two authors could work in the same "family × information source × instrument × horizon" cell.

### Release chain

From 2026-11-04 every platform release went through nine steps:

1. the platform writes a plan;
2. the reviewer checks the plan's facts against the code;
3. the lead makes a decision with an acceptance criterion;
4. the platform writes the code;
5. the coordinator checks the kit and runs the full test suites while the reviewer reviews;
6. the coordinator merges if the criterion is met;
7. the lead agrees an install window and the owner gives consent in the operator's chat;
8. the operator installs the release between rounds;
9. the owner presses Start.

The typical criterion: no high or medium findings in the path of orders, positions, protection, accounting and recovery; every planned item done or explicitly deferred; old exports unchanged byte for byte; full test suites pass.

### Round chain

1. The lead and the owner plan the round.
2. The operator runs the checks and writes the Start card.
3. The owner presses Start.
4. The operator checks the round read-only at about 35 minutes and during the first two hours.
5. Within about an hour of the end, the operator collects the evidence package and the coordinator checks its hashes.
6. A quiet round is closed with a helper receipt. A round with a fill, an anomaly, a pause or an incomplete journal, and the first round on a new release, go to the reviewer.
7. The judge decides whether the round counts toward gate D.

### Owner-only actions

Only the owner could start, stop or resume any round (Live, Demo or paper), restart the production panel, run the root launch scripts or live entry points, place orders, move funds, or change account and key settings. The charter, every brief and the guard hook repeated this. The operator's consent rule is described above.

### The guard hook

The guard is a Node.js `PreToolUse` hook (`guard.cjs`). Claude Code runs it in every session before each shell command, file access, browser action and publishing call.

- **Owner actions:** blocks the Start, Stop and Resume routes, the live entry points, the root launch scripts and the panel restart.
- **Git:** blocks force pushes in any form, mirror and prune pushes, deleting remote branches, `reset --hard`, `clean -f`, changes to remotes, Git configuration, hooks and credential helpers, and Git environment overrides (some of them, such as tracing, can print the GitHub token).
- **Secrets:** blocks reading `.env` files, local settings, private state archives, `.netrc`, `.ssh` and Git credentials, in any letter case.
- **Roles:** a marker file in a role's worktree sets the role, and the session's transcript path pins it, so changing directory does not escape it. Authors cannot read the sealed data vault, download market data, write to the shared data store, call Jev or OpenRouter directly, or move `main`, tags or refs. The operator and the reviewer cannot change code. Of the sessions that carry a role marker, only the operator can use the owner's Chrome, which holds the remote-desktop tab.
- **Parsing:** commands are read the way a shell would read them, including quotes, nested `bash -c` and PowerShell. URLs, typed text and scripts in browser tools are checked like commands. Commands longer than 100,000 characters are blocked.
- **Tests:** the regression suite grew from 49 cases on the first day to 339 by version 4 of the guard (2026-11-04).

Limits, stated plainly: the guard is a first line, not a sandbox. If the guard itself fails, it allows the call. It cannot see clicks by screen coordinates, so the operator's click rules covered that gap. The real money limit for paid model calls was the credit limit on the API key, set by the owner. Only the lead could edit the guard, and only the owner committed the settings file that wires it in. A role that thought a block was wrong wrote BLOCKED for the coordinator instead of working around it.

### Paid model calls

Jev was an optional tool for authors. Paid research calls went only through the platform's research harness, which kept a separate ledger per author, stopped hard at the $1.00 cap, blocked calls when billing was unknown, and kept absolute dates, times and prices out of numeric inputs so the model could not "remember" history. Paid mode stayed off until the coordinator enabled it for a reviewed request specification.

### One shared laptop

All sessions ran on one laptop. Heavy evaluations took a shared lock and, later, a booking with the coordinator: one at a time for the whole team, and only with at least 6 GB of free memory. Full test suites did not run while the operator was using the remote desktop.

## Numbers

Counted in the working repository at the end of the team era.

| What | Value |
|---|---|
| Team era | 2026-10-28 (owner decisions D093) to 2026-11-17 (decision D204: wind down trading, publish as open source) |
| Long-running AI sessions | 11: lead, coordinator, platform, four authors, the owner's slot MYHuman, judge, reviewer, operator |
| Helper agent definitions | 5, on Claude Haiku |
| Commits on `main` in the working repository | 1,409 from the team setup commit on 2026-10-28 up to the commit that recorded decision D204 on 2026-11-17, including 362 merge commits |
| Lead decisions | 119 DECISION entries in the lead's channel, numbered D093 to D204 (some with letter suffixes such as D100a) |
| Channel entries | 552 across the 11 role channels |
| Owner action queue | 40 numbered items |
| Platform releases | 27 entries in the release notes, from 2026-10-28 to 2026-11-17 |
| Review files | 58: 19 release-candidate reviews, 10 Live-round reviews, receipts and lead checks, 5 Demo-round reviews, 7 fact checks of platform plans, 8 on the Jev research harness, news mode and model card, 4 independent reproductions of the candidate's numbers, 5 review plans and design or procedure reviews |
| Release candidates not accepted at first review | R2.1.5 rejected; R2.1.6 kept off the trading PC; the first candidate of each of R2.1.10 to R2.1.15 sent back and accepted only as a second candidate |
| Guard regression cases | 49 on the first day, 339 at version 4 |
| Pre-registration files | 81 in the five authors' PREREG folders |
| Trial ledger lines | 550 in the five authors' ledgers |
| Live runs on real funds | 11 started between 2026-10-30 and 2026-11-17: two 5-hour rounds of four V1 bots (30 and 31 October), one 300-second order-path probe, two 24-hour rounds of the single candidate bot, and six 24-hour rounds of five bots (BTC and ETH, with SOL added from the third of them). The last of these, at 3x leverage, was the final round. |
| Live rounds that paused | 2 (the five-bot rounds that started on 2026-11-09 and 2026-11-13 UTC); both ended when the owner pressed the recovery button |
| Release installs by the operator | 6, between rounds |

## What worked

- **Files in Git as the team's memory.** Every role wrote to its own append-only channel, and the board, the owner's queue, the briefs and the ledgers lived in Git. Sessions were restarted and cleared during the project, and the chats are archived at the end, but the record stays. Reviewers started each large review in a fresh context and kept their state in files.
- **One integrator.** Only the coordinator merged into `main` and pushed it, with path, secret and test checks on every merge.
- **Independent review with the criterion set in advance.** Reviews found real defects in the code that handles orders and protection before it was installed; see the table below. A release that was not accepted came back as a second candidate. For the leverage release, for example, each fix was confirmed by a probe that had exposed the defect in the first candidate.
- **Pre-registration, the trial ledger and one look at held-out data.** They turned good-looking backtests into honest negatives. Two examples:
  - Sol's channel-breakout cycle (indicative numbers from the author's own engine): all 24 configurations were positive on DEV, and the selected one made +95.5 USDT on 55 trades. On its single look at the tail it made −0.79 USDT on 11 trades. No candidate.
  - One of Opus's lines, on its single look at the unopened tail: −2.46 USDT net and −7.57 USDT under stress. The line was closed for good.
  
  A random-entry control showed that, in the long-horizon tests, most of the DEV profit came from the market going up rather than from timing: random long entries with the same holding times made 462 bps per trade, against 622 bps for the actual entries.
- **A mechanical boundary.** The guard blocked actions instead of relying on instructions. When the guard, or Claude Code's own auto-mode classifier, blocked something, sessions reported it and did not look for a way around. Two examples: the guard stopped an author from reading the file that enables paid calls; the classifier stopped the coordinator's first push to GitHub, and the coordinator asked for an owner rule instead of retrying in another form (push was then pre-approved by decision D123).
- **Money stayed with the owner.** In all seven rounds of the operator's register the owner pressed Start, and the operator filled in the form only with the owner's consent. Both paused rounds were closed out by the owner pressing the recovery button, not by an agent.
- **Splitting roles when the lead got overloaded.** The reviewer and the operator were added on 2026-11-04 (decision D137), after a review of the team's structure showed that the lead's context was full of remote-desktop work and reviews.
- **Moving supervision out of the model.** After session-based supervision failed (see below), the platform built a small notifier that runs next to the panel on the trading PC. It uses no AI model and no usage limits, only reads the panel, and sends the owner a Telegram message on round start, pause, manual review, long degraded windows, the end of the entry window, the end of the round, and panel outages (decision D167).

## What did not work

- **Weekly usage limits.** From 2026-11-05 to 2026-11-08 the whole team stood still because the weekly model usage limit was used up. In the same days, a 24-hour Live round that had started on 2026-11-05 had no supervision after its first two hours, and its evidence package was collected on 2026-11-08 instead of within an hour of the end. Earlier the team had also adopted a rule that an author with no useful work should end its turn and wait, to save the owner's limits.
- **Supervision by an AI session.** In four of the seven rounds in the operator's register, planned checks did not happen. Once it was in the days when the team was stopped by the weekly limit. Three times the session-based timers failed: a timer fired but the check was not run and no new timer was set; a timer ended without firing when the session was restarted; a scheduled job disappeared with a session restart. A pause on 2026-11-10 at 15:41 UTC went unnoticed until the lead asked the next morning. The notifier alone was not enough either: on 2026-11-14 its pause alert arrived on time, but nobody reacted for 46 hours.
- **Clock times.** Sessions often wrote estimated times in their channel headers instead of reading the clock. Eight of the eleven role channels contain corrections of their own timestamps, some of them off by a whole day. The rule became: every time and date comes from the system clock (`date`).
- **Cross-session communication.** Sessions saw each other only through files and short messages. Examples from the record:
  - the coordinator started a market-data download that the platform had already started, and stopped its copy a minute later;
  - the operator used "No. 36" for a season while the owner's queue used that number for something else; after that, rounds were named only by run id and numbers came only from the owner's queue;
  - messages could arrive late: the judge received the coordinator's messages after it had already posted its own update;
  - the messaging tool could not address a session by its short role name, so the protocol had to list the full session names;
  - a number copied from a review into a decision (an order-size limit) turned out to be wrong when checked against the code, and the decision was corrected;
  - the platform withdrew an install kit that had already been merged, and had to restore it.
- **One shared laptop.** Heavy runs queued behind each other. On 2026-10-29 a heavy run stopped itself when free memory fell below the 6 GB threshold, and the second parallel slot was switched off again.
- **No edge.** The process produced honest negative results, but no strategy reached "proven". The only candidate that passed gate K (H10, on ETH) completed no trade in the seven finished 24-hour Live rounds it took part in, so none of them counted toward gate D. At the judge's last evaluation its gate D sample had 1 of 30 declared trades. Live data on the older V1 bots showed the core problem: they caught 0 to 5 bps of gross movement per trade, against 10 bps of taker costs per round trip.

## Defects stopped before installation

| Release | What was found | Why it mattered |
|---|---|---|
| Team tools, 2026-10-28 | An independent sub-agent review of the shared evaluator found two bugs: the clone check diluted entry overlap when two periods differed, and the evaluator could pass silently without a funding policy. Both fixed with tests. | Gate verdicts depend on this evaluator. |
| R2.1.4, checked on 2026-11-03 | An offline check before the first real-money order probe found that a rejected or lost order paused the round with no way out through Resume. A race between the local stop-loss and the exchange stop led into the same trap, and there was no cooldown after an exchange stop. | The order path had not yet run once on real funds. Fixed in the next release. |
| R2.1.5, rejected | A lost reply to a market close or an IOC entry made the engine see a "foreign" position and pause, leaving an IOC lot without an exchange stop. An older defect: the final reconciliation re-read cancelled orders that the exchange forgets after about two hours, so a 24-hour round could never finish. | Both would have hit the planned 24-hour rounds. |
| R2.1.6 | Two medium findings in the fixes. | Kept off the trading PC; replaced by later releases. |
| R2.1.10, first candidate | Two medium findings in the recovery path. | Fixed in the second candidate. |
| R2.1.13, first candidate | A new evidence-archive check would have rejected every mixed-season archive written by the release then installed on the trading PC. | The evidence of the next round would have failed the standard check. |
| R2.1.14, first candidate | Two medium findings in the position and recovery path. | Fixed in the second candidate. |
| R2.1.15 (per-bot leverage), first candidate | High: a persistent failure to place a stop led to an endless loop with no stop on the exchange, and the bot could enter again after a rule-based close. Plus two medium findings in protection and recovery. | Found before the first 3x-leverage round. The second candidate fixed all three. |

The first candidates of R2.1.11 and R2.1.12 were sent back for a different reason: planned items were neither done nor explicitly deferred (and, in R2.1.12, one interface regression).

## Incidents that reached Live

| Round | What happened | Exposure | Response |
|---|---|---|---|
| First 24-hour test round of the candidate, ended 2026-11-03 | The journal ended incomplete after repeated exchange error codes and market-data gaps. | No order was placed. | Release R2.1.10 handled the exchange code and other temporary codes. |
| Five-bot round started 2026-11-09, paused 2026-11-10 (UTC) | An exchange-side stop closed a position during an account check. The engine did not recognise the fill as its own, reported a position mismatch and paused all five bots for manual review. | No actual position was open during the pause; the arena still showed the last known one. | The owner pressed recovery the next morning. Fixed in R2.1.14. |
| Five-bot round started 2026-11-13 (UTC) | The executor process crashed, most likely because the trading PC's disk had filled up, and the round paused. | No open positions. | The owner pressed recovery 46 hours later. Free disk space became part of the pre-start checks. |

## Outcome

From 2026-10-28 to 2026-11-17 the team worked with clear roles, written rules and a full record. The rules kept money actions with the owner, stopped faulty releases, and recorded failed ideas as failures. Supervision of Live rounds by AI sessions did not hold up and was moved to a plain program, which still needed a human to react. None of this could make a strategy profitable. On 2026-11-17 the owner wound trading down. The last round, which had already started, was left to finish, the remaining planned work was cancelled, and the project was prepared for publication.
