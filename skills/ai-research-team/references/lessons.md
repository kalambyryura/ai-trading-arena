# Lessons from the reference team

From 28 Oct to 17 Nov 2026 one human owner (kalambyrYura) and a team of Claude Code sessions researched crypto perpetual-futures strategies in the ai-trading-arena repository, with live rounds on real funds started by the owner. The full story is in that repository's `docs/AI_TEAM.md`. This file keeps what is useful for the next team.

## Contents

1. The team in numbers
2. What worked
3. What did not work
4. Defects stopped before installation
5. Incidents that reached live runs
6. Rules that follow

## 1. The team in numbers

| What | Value |
|---|---|
| Long-running sessions | 11: lead, coordinator, platform, four authors, the owner's own strategy slot (MYHuman), judge, reviewer, operator |
| Helper agent definitions | 5, on the smallest model |
| Commits on `main` in the working repository | 1,409, including 362 merges |
| Lead decisions | 119 DECISION entries |
| Channel entries | 552 across 11 channels |
| Owner action queue | 40 numbered items |
| Platform releases | 27 entries in the release notes |
| Review files | 58 (19 release-candidate reviews, 10 live-round reviews, 7 plan fact checks, 4 independent reproductions of the candidate's numbers, and others) |
| Guard regression cases | 49 on the first day, 339 at version 4 |
| Pre-registrations | 81 files; 550 trial-ledger lines |
| Live runs on real funds | 10; 2 paused, both closed out by the owner pressing the recovery button |
| Strategies proven | 0 |

## 2. What worked

- **Files in git as the team's memory.** Sessions were restarted and cleared during the project; the channels, the board, the owner queue, the briefs and the ledgers kept the state. Reviewers started each large review in a fresh context and kept their state in files.
- **One integrator.** Only the coordinator merged into `main` and pushed it, with path, secret and test checks on every merge.
- **Independent review against a criterion set in advance.** Reviews found real defects in the code that handles orders and protection before it was installed (section 4). A release that was not accepted came back as a second candidate, and each fix was confirmed by the probe that had exposed the defect.
- **Pre-registration, the trial ledger and one look at held-out data** turned good-looking backtests into honest negatives (references/research-cycle.md section 8). A random-entry control showed that most long-horizon profit on development data was the market going up.
- **A mechanical boundary.** The guard blocked actions instead of relying on instructions, and when the guard or the client's own classifier blocked something, sessions reported it and did not look for a way around.
- **Money stayed with the owner.** In every round of the operator's register the owner pressed Start; the operator filled in the form only with the owner's consent; both paused rounds were closed out by the owner.
- **Splitting roles when the lead got overloaded.** The reviewer and the operator were added after a review of the team's structure showed that the lead's context was full of remote-desktop work and reviews, and that the lead was checking its own specs.
- **Moving supervision out of the model.** After session-based supervision failed, the platform built a small notifier next to the trading panel: no AI model, no usage limits, read-only, a message to the owner on start, pause, manual review, long degraded windows, end of the entry window, end of the round and panel outages.

## 3. What did not work

- **Weekly usage limits.** From 5 to 8 Nov 2026 the whole team stood still because the weekly limit was used up: about ten sessions, most on the strongest model at maximum effort. A 24-hour live round in those days had no supervision after its first two hours, and its evidence was collected days late. Reviews and planned work waited for the reset.
- **Supervision by an AI session.** In four of the seven rounds in the operator's register, planned checks did not happen: once during the usage-limit stop, and three times because session timers failed (a timer fired but no check ran and no new timer was set; a timer ended without firing when the session restarted; a scheduled job disappeared with a restart). A pause went unnoticed until the lead asked the next morning. The notifier alone was not enough either: one pause alert arrived on time and nobody reacted for 46 hours.
- **Clock times.** Sessions often wrote estimated times; eight of the eleven channels contain corrections of their own timestamps, some off by a whole day.
- **Cross-session communication.** A data download was started twice (the copy was stopped after a minute); a decision and a later chat message named different roles for the same job; messages arrived after the receiver had already acted; short role names did not route; a consent relayed by another session was not valid; a number copied from a review into a decision was wrong when checked against the code; an already merged install kit was withdrawn and had to be restored.
- **Workspace trust after an app restart.** After the desktop app restarted, sessions in their own worktree folders did not start until the user confirmed trust for each folder; their final tasks fell to the lead working alone with helpers.
- **Too many roles too early.** Six sessions started on day 0, before the shared evaluator, the data store and the SDK existed: authors built private engines, filed overlapping feature requests and then sat idle. Roles added later on proven need (judge, reviewer, operator) worked well.
- **One shared laptop.** Heavy runs queued behind each other; one stopped itself when free memory fell below the 6 GB floor, and a second parallel slot was switched off again. The laptop once froze hard while remote-desktop work and a full suite ran together.
- **Append-only files grew huge.** The board reached about 160 KB, the lead's channel about 280 KB, the release notes about 350 KB, and the board's "last update" header went stale.
- **No edge.** The process produced honest negatives, but no strategy reached "proven". The fast bots caught 0 to 5 bps of gross movement per trade against 10 bps of taker costs per round trip; on real money, 86 trades lost 1.65 USDT net: price moves earned +0.07 USDT and fees took 1.71 USDT.

## 4. Defects stopped before installation

| Release | What was found | Why it mattered |
|---|---|---|
| Team tools, day 1 | An independent subagent review of the shared evaluator: the clone check diluted entry overlap when two periods differed, and the evaluator could pass silently without a funding policy | Gate verdicts depend on the evaluator |
| Order-path release before the first real-money probe | A rejected or lost order paused the round with no way out through Resume; a race between the local stop and the exchange stop led to the same trap; no cooldown after an exchange stop | The order path had never run on real funds |
| A rejected release | A lost reply to a market close made the engine see a "foreign" position and pause, leaving a lot without an exchange stop; a final reconciliation re-read cancelled orders the exchange forgets after about two hours, so a 24-hour round could never finish | Both would have hit the planned 24-hour rounds |
| Several first candidates | Medium findings in recovery, position and protection paths; an evidence-archive check that would have rejected every mixed-season archive written by the release then installed | Fixed in second candidates |
| Per-bot leverage, first candidate | High: a persistent failure to place a stop looped forever with no stop on the exchange, and the bot could re-enter after a rule-based close | Found before the first higher-leverage round |

## 5. Incidents that reached live runs

| Run | What happened | Response |
|---|---|---|
| First 24-hour test round of the candidate | The journal ended incomplete after repeated exchange error codes and market-data gaps; no order was placed | A release handled the exchange code and other temporary codes |
| A five-bot round | An exchange-side stop closed a position during an account check; the engine did not recognise the fill as its own, reported a mismatch and paused all five bots for manual review | The owner pressed recovery the next morning; fixed in a release |
| Another five-bot round | The executor process crashed, most likely because the trading machine's disk had filled up; no open positions | The owner pressed recovery 46 hours later; free disk space joined the pre-start checks |

## 6. Rules that follow

1. Start with the lead, the coordinator, the platform and one or two authors; add roles on their triggers.
2. Put the coordinator on a mid-tier model and routine work on helpers; end turns when nothing useful is left; never duplicate runs; keep state in files so work resumes after a reset; plan big work around the reset date. Do not slim reviews to save limits.
3. Keep live supervision in a plain program that alerts the owner, and agree who reacts and how fast.
4. Read the clock in the same command that writes a time; run `date` before every decision.
5. Commit the PREREG before the run, never after.
6. Read the channels and BOARD, then CLAIM, then start.
7. Files win over messages; ask the lead for a new entry when they disagree.
8. Address sessions by exact title or id; consent counts only in the acting role's own chat.
9. Run full suites on a `git archive` snapshot, never in someone's live worktree.
10. Before a planned app restart, finish or hand off in-flight work and commit; after it, confirm trust for every worktree folder, check each session's model and effort, and keep end-of-project steps doable by the lead alone.
11. A merged and verified kit stays as it is; new code goes into a new candidate.
12. Keep BOARD current-state only and archive by month; keep the hot cache under 8 KB.
13. Ask the owner with concrete scenarios, not abstract policies.
14. Check models before expensive work; a session can silently run on a different model after a safety fallback.
15. Short-horizon strategies need a median gross move of at least twice the round-trip cost; compute long and short separately; random-entry controls with the same sides and holding times are mandatory; a filter based on a paid language model must be tested for symmetry and structure first.
