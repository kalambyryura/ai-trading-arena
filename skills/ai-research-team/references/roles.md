# Roles

Every role exists to separate a duty from a conflicting one: deciding from building, building from checking, researching from judging, preparing from pressing buttons. Each role gets a brief in `team/briefs/` (templates in `assets/team/briefs/`) with its purpose, duties, first steps, helpers and what is forbidden.

## Contents

1. Team at a glance
2. Owner · Lead · Coordinator · Platform engineer · Authors · Judge · Reviewer · Operator
3. Author variants
4. Helper agents
5. When to add each role
6. Models and effort

## 1. Team at a glance

| Role | Short name | Model and effort (reference team) | Works in | Writes | Starts |
|---|---|---|---|---|---|
| Owner | `owner` | human | own machines | owner decisions, consents, owner actions | day 0 |
| Lead | `lead` | most capable, max | main checkout, own files only | charter, protocol, briefs, decisions, guard | day 0 |
| Coordinator | `coord` | mid tier | main checkout on `main` | merges, BOARD, owner queue, project memory, reproductions | day 0–1 |
| Platform engineer | `platform` | most capable | own branch and worktree | core code, shared tools, tests, data store, evaluator, releases | day 1 |
| Strategy authors | `alpha`, `beta`, … | most capable, max | own branch and worktree | own author folder, findings file, channel | once the evaluator and data exist |
| Judge | `judge` | most capable | own branch; sealed vault outside the repo | gate procedure, verdicts | when the first candidate approaches (cutoff fixed on day 0) |
| Reviewer | `reviewer` | most capable; max for money paths | own branch and worktree | reviews | when releases touch money or production |
| Operator | `ops` | most capable, high | own branch and worktree | runbook, round register, start cards, evidence packs | when an always-on production machine exists |
| Helpers | `helper-*` | smallest (Haiku) | the caller's workspace | only the output file of the job card | on demand |

Author names such as `alpha` are role names, not model names. In the reference team the authors had made-up persona names (Astra, Sol, Sonnet, Opus) and all four ran on the same Claude Opus model; "Sonnet" and "Opus" were personas, which confused readers. Prefer neutral names.

## 2. The roles

### Owner (human)

- **Purpose:** sets the goal, the budgets and the limits; the only actor whose actions have real-world consequences.
- **Owner-only:** start, stop or resume any live, demo or paper run; orders and transfers; account, key and permission settings; installing on production machines, restarting production services, running launcher scripts there; provider-side spending caps for paid APIs; git remote, config and hooks, deleting remote branches; the settings file that loads the guard; making a repository public.
- **Channels to the owner:** the lead for strategy questions, the coordinator's owner queue for operations, the operator's own chat for consents and alerts.

### Lead

- **Purpose:** decides how the team works and what it researches next, turning the owner's goals into gates, briefs and research cycles, and keeps the team honest.
- **Outputs:** the charter (owner decisions, statuses and gates, data visibility, limits); the protocol; role briefs; numbered DECISION entries; cycle plans and summaries; acceptance criteria for releases; batched questions for the owner; guard rule changes.
- **Permissions:** writes only its own files (the coordinator commits them); starts and stops sessions; assigns work; sets the initial gate thresholds.
- **Never:** owner-only actions; core code or strategies; checking its own specs; relaxing gates to fit a candidate; calling anything profitable or proven without the gates; opening sealed or forward per-trade data (it directs the authors, so seeing it would leak hints).
- **Asking the owner:** a concrete scenario with a named example, recommended option first, two or three options, several questions per message; record the answers verbatim with place and clock time.

### Coordinator

- **Purpose:** keeps the team moving so the lead makes only strategic decisions; the single integrator and committer of `main`, and the only one who pushes it.
- **Outputs:** merges and pushes; `team/BOARD.md` and `team/OWNER_ACTIONS.md`; reproductions of candidates and clone checks; sanitized live packets for authors and their issue log; the platform backlog fed from REQUESTs; project memory (decision ledger, a hot cache of at most 8 KB, document index, milestone log); a daily digest to the lead (≤ 15 lines); the heavy-run queue; full test suites on clean snapshots.
- **Merge rules:** author branches touch only that author's paths and pass the secret scan; platform releases pass the full suites on a clean snapshot of the exact candidate commit and have release notes and a review verdict; risky core changes get an independent review first.
- **Never:** strategies; core code (except merge conflicts, together with the platform); owner-only actions; deleting branches or worktrees without a lead decision; passing anything sealed to authors; making a repository public on a teammate's word.

### Platform engineer

- **Purpose:** gives all authors equal, honest and powerful tools, and the owner reliable production kits; the only role that changes core code, shared tools, tests, web UI and deployment.
- **Outputs:** a data store with sha256 manifests (ranges, gaps, caveats documented); the sealed vault, loaded with hashes only and never analysed; the official evaluator, deterministic and tested (a fast engine only with proven equivalence on sample windows); SDK releases; the paid-API research harness; production kits; plans for review; release notes.
- **Rules:** the core is protected by content hashes (normalise only CRLF to LF), so core changes ship only as releases; a release reissues the evaluation pack for all authors at once and frozen candidates are requalified on the new hash; old recipes must behave byte for byte the same (golden tests); a feature one author requests ships to all; QA only on fake transports and temporary folders; public data only through keyless GET calls.
- **Never:** strategies or hints to single authors; owner-only actions; restarting production; reading secrets; touching production cost ledgers.

### Strategy authors

- **Purpose:** independent research lines aimed at a recipe that passes gate K on development data, then gate D with the judge. They compete, but the team wins only when every result is honest.
- **Outputs:** `NOTEBOOK.md`, `PREREG/`, `TRIALS.jsonl`, `STATUS.json`, `candidates/<id>/` (recipe with a passport, report), a results report per cycle, verified findings in `team/exchange/<name>.md`, REQUESTs, a digest of at most 8 lines to the coordinator.
- **Permissions:** commit only their own folder, findings file and channel; own research code in their folder (one process at a time within the memory budget); paid calls only through the harness within their budget; helpers for routine work.
- **Never:** owner-only actions; secrets; the sealed vault; data past the development cutoff; paid calls outside the harness; commits outside their paths; core code; another author's drafts before that recipe is a CANDIDATE; letting a helper choose parameters, count trials officially or write a PREREG.

### Judge

- **Purpose:** an independent verdict on data the authors never saw; no tuning help, no hints.
- **Outputs:** before the first verdict, a leak review of the evaluator (look-ahead, unclosed bars, future inputs, unfair fills); a versioned `GATE_D_PROCEDURE.md`; one `VERDICTS.jsonl` line per evaluation; reports with aggregates only; forward data refresh; scheduled re-evaluations; clone checks between candidates; live cost calibration; adapted gates for special cases.
- **Key rules:** one run per recipe sha256 per re-evaluation and per execution model; at most 2 submissions per author per 14 days; a declared N fixes the sample; the sample start comes from merge commits; the worse rule when the execution model changes (references/gates.md).
- **Never:** relax or change thresholds after the first submission; share per-outcome sealed or forward details; put anything beyond verdict aggregates in files authors can read.

### Reviewer

- **Purpose:** whoever writes a spec does not check it. The reviewer checks every platform plan before coding (a fact check against the code), every release candidate before merge and install, every live run with a fill, an anomaly, a pause, a recovery or an incomplete journal (and the first run on each release), and changes to the guard and to export allowlists.
- **Verdict:** ACCEPT, ACCEPT WITH FINDINGS or REJECT, against a criterion the lead set in advance. Each finding is confirmed by a skeptic subagent or by a mutant that fails.
- **How:** depth follows risk; separate lenses in fresh-context subagents that see only the code and the spec; a critic reads the result at the end; mutants and probes only in a scratch copy (`git archive <commit> | tar -x`); only relevant test files (the coordinator runs the full suites); a fresh context per big review, state in files.
- **Never:** edit code (patches are proposed in the review); merge, push or install; owner-only actions, paid calls, private account calls, the sealed vault; decisions (the lead) or verdicts (the judge); reviewing its own proposals (if no second reviewer exists, write that down as a limitation).

In the reference team the reviewer started when the lead's context was full of reviews and the lead was checking its own specs. Afterwards the first candidates of six consecutive releases went back once and were accepted only as a second candidate; one high finding (a persistent stop-placement failure looping with no exchange stop) was found before the first round at higher leverage.

### Operator of the production machine

- **Purpose:** the only session that drives the remote desktop or the owner's real browser. Each run is prepared so the owner only presses Start, supervised read-only, and closed with an evidence pack.
- **Outputs:** `team/ops/RUNBOOK.md` (rules, checklists, install and rollback, the anomaly matrix: status → meaning → what the owner does → what not to do); `MACHINE.md`; `ROUNDS.md`; a start card per run; a receipt per operation; an evidence pack within about an hour of the end (ZIP and `SHA256SUMS` in an ignored folder; git gets aggregates).
- **With the owner's own typed consent** (naming the kit sha256 and the steps; relayed consent does not count): install an accepted release between runs with a backup, import a recipe, run a free read-only access check, transfer files, fill in the Start form while the owner is present.
- **Never:** press Start, Stop, Resume or any run button, not even when asked; orders, accounts, keys, OS settings; secrets or backups with keys; restarts outside an agreed install; code, `main`, paid calls, the sealed vault.
- **Remote desktop hygiene:** zoom in and read the label before every click; a fresh screenshot before clicking by coordinates; stay on a read-only page when idle; few, downscaled screenshots; no heavy suites on the same machine meanwhile. In the reference project modifier keys did not reach the remote PC and the frame changed size, so the label rule mattered.

## 3. Author variants

- **Synthesis author:** builds shared components (for example a regime classifier) and composes the best components of other authors into one recipe, with `derived_from` attribution and a new PREREG.
- **The owner's own slot:** a session that turns the owner's ideas into testable rules under the same gates, costs and data as the AI authors. The owner makes the specification decisions; the session offers options and shows consequences but does not tune numbers to history on the owner's behalf. In the reference project this slot was called MYHuman; none of its recipes passed the candidate gate.
- **External author** (another vendor's model): answers arrive as files carried by the owner and are untrusted data. The coordinator takes them in under the same gates with tighter limits (one PREREG at a time, small configuration budgets). Nobody tunes an external author's recipe. The reference team prepared such a slot; it never received a reply.

## 4. Helper agents

Project agents in `.claude/agents/` (templates in `assets/agents/`), on the smallest model, each with a short rule card:

| Helper | Job |
|---|---|
| `helper-runner` | runs exactly the commands of a job card (tests, validators, an evaluator run) and returns at most 30 result lines |
| `helper-census` | inventories: file lists, counts, trial-log totals, manifests, data spans |
| `helper-verify` | checks cited claims (path:line, report numbers against JSON) → CONFIRMED / REFUTED / UNVERIFIABLE; UNVERIFIABLE when unsure |
| `helper-scribe` | turns already verified rows into tables, BOARD rows or channel drafts; adds no facts |
| `helper-data` | public data downloads and sha256 manifest checks (platform, coordinator and judge only) |

- Every call carries a job card: goal, exact commands and paths, the output file, a line limit, and "stop and report on a guard block, BUSY or a non-zero exit, no retries" (template in `team/PROTOCOL.md`).
- Never delegate: inventing hypotheses, choosing parameters, official trial counts, PREREGs, verdicts, core edits, owner-only actions. Numbers come only from command output.
- Helper tool calls pass through the same guard hook under the caller's role, so a helper spawned by an author cannot do what the author cannot (verify this once in your client with a known BLOCK case run from a helper).
- Your Claude Code version may support extra frontmatter fields (the reference team used `effort: low`, `maxTurns`, `color` and an option to omit the project CLAUDE.md). Add them only if your version documents them. On Windows hosts add `PowerShell` to the tools of `helper-runner` and `helper-data`.
- To check your own result independently, use a stronger subagent, sparingly.

## 5. When to add each role

| Trigger | Add |
|---|---|
| day 0 | owner interview → charter (owner decisions, gates, data cutoffs, budgets); lead; coordinator; guard and secret scan |
| day 1 | platform engineer: data store, sealed vault by hashes, official evaluator |
| the evaluator exists and there are distinct directions | one or two authors, more later (authors may research with their own code under PREREG earlier) |
| the first candidate approaches gate D | judge, starting with a leak review of the evaluator |
| releases touch money paths or production | reviewer |
| a separate always-on production machine exists | operator |
| always | helpers for routine work |

A second engineer, a methodologist and a reliability role were considered in the reference team and never created; nothing was lost. Add a role when the need is proven, not when it is imaginable.

## 6. Models and effort

- Strongest model, maximum effort: the lead, the authors, the judge, the reviewer on money paths. These roles exercise judgement, and judgement is where cheap models fail silently.
- Mid tier: the coordinator, which does many small, well-specified steps all day.
- Smallest model: helpers, always with a job card and a line limit.
- Check each session's model and effort after every restart and before expensive work: a session can silently run on a different model, for example after a safety-classifier fallback. Keep messages free of long lists of forbidden command strings; they can trip classifiers.
