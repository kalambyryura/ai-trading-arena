# Protocol: how the team works together

The scaffolded `team/PROTOCOL.md` is the short, binding version for the team. This reference explains the reasons and the formats behind it.

## Contents

1. Ten rules that carry everything else
2. File layout
3. File formats
4. Communication
5. Git
6. The release flow
7. Live or production runs
8. Resources
9. Checklists

## 1. Ten rules that carry everything else

1. **Files in git are the record.** Messages between sessions are only signals that point to a file entry. Sessions restart, lose context and receive messages late; git keeps the record and lets a new or remote session catch up with a pull.
2. **One integrator.** Only the coordinator commits to `main` and pushes it. One place where all branches meet, with the same checks every time.
3. **The owner alone touches the real world:** money, accounts, keys, production machines and publication. Agents prepare; the owner presses.
4. **Register before you test.** A PREREG and its TRIALS line are committed before any run; every attempt is logged, failed and interrupted ones included. Otherwise the number of tries, and with it the chance of a lucky result, is invisible.
5. **Research uses development data only.** Held-out data stays with the judge. A held-out tail gets one look.
6. **The judge is independent** and returns only PASS / FAIL / INSUFFICIENT plus aggregates, so nobody can tune against it.
7. **Whoever writes a spec does not check it.** A reviewer checks plans, releases and live runs against a criterion the lead set in advance.
8. **Times come from the system clock,** read in the same command that writes them.
9. **Honest labels beat good-looking numbers.** "No candidate" is a valid result. Nothing is called profitable or proven without passing the gates.
10. **Start small.** Add a role when the need for it is proven.

## 2. File layout

```
team/
  README.md  CHARTER.md  PROTOCOL.md  BOARD.md  OWNER_ACTIONS.md  LIVE_PACKETS_LOG.md
  briefs/            role briefs (lead)
  channel/<role>.md  append-only, one file per role
  exchange/<author>.md  verified findings for everyone
  authors/<name>/
    NOTEBOOK.md  STATUS.json  TRIALS.jsonl
    PREREG/<id>.md   candidates/<id>/   research/   <cycle>/RESULTS.md   live/
  platform/          BACKLOG.md  RELEASES.md  DATA.md  <REL>_PLAN.md  designs
  judge/             GATE_D_PROCEDURE.md  VERDICTS.jsonl  SEALED_1.json  FORWARD.json  LIVE_ROUNDS.jsonl  tools/
  reviews/           <REL>_PLAN_CHECK.md  <REL>_REVIEW.md  <REL>_RC2_REVIEW.md  <RUN>_REVIEW.md  <CAND>_REPRO.md
  ops/               RUNBOOK.md  MACHINE.md  ROUNDS.md  START_CARD_<run>.md
  tools/             guard.cjs  guard.config.json
```

Data stores, the sealed vault, `runtime/` outputs and logs live outside git (ignored, or outside the repository). Write the team documents in the team's working language; keep public summaries in English if they will be published.

## 3. File formats

### README, charter, protocol

- **README:** mission of the stage, a who-is-who table (role, session title, model, workspace, what it writes), reading order, absolute limits.
- **Charter** (lead; versioned with a change log naming decisions): why we are here and what is known; owner decisions G1…Gn, which change only with the owner's consent; statuses and gates; data partitions and who sees what; competition and sharing rules; paid-API rules, execution limits, participants and provenance; evidence-backed lessons.
- **Protocol:** git, communication, resources, helpers, file layout, guard.

### BOARD (coordinator)

- Header: "Updated by coord. Last update: `<clock time>`, `<what changed>`". Update it at every change and at least daily.
- Sections: roles (role · session id or title · branch/worktree · current task · status); production machine (installed release, hashes, backup, service state, active run, next install window, last evidence); participants (live version · best candidate · status · submissions in the last 14 days); heavy-run queue; family occupancy (author · family × information source × instrument × horizon · date chosen); blockers; git hygiene; independent checks of `main` (date, commit, suite counts).
- Current state only. In the reference team the board grew to about 160 KB and its "last update" header went stale; archive history by month.

### OWNER_ACTIONS (coordinator)

`| # | Action | Why | Status |`, statuses WAITING · DONE · CANCELLED · LATER. Each action has exact steps, file names and hashes. Numbers are never reused. In the reference team an operator used "No. 36" for a round while the owner queue used 36 for something else; after that, runs were named only by run id and numbers came only from the owner queue.

### Author folder

- **`NOTEBOOK.md`** (append-only): header with author, model and branch, then dated sections `## <date> · <cycle or hypothesis> · <one-line outcome>`. Day 1: what was taken from earlier work, the starting conclusion, 3–7 starting hypotheses (idea, why it can beat the costs, data and tools needed, how to refute it). Claims marked [VERIFIED], [ASSUMPTION] or [OPEN].
- **`STATUS.json`:**

  ```json
  {"author": "alpha", "status": "DRAFT", "branch": "team/alpha", "current_candidate": null,
   "live_version": "V1 (recipe sha <hash prefix>)", "updated_at": "2025-03-14T15:15:33Z",
   "phase": "cycle C2 closed, no candidate; idle until a new assignment",
   "configs_tested": 60, "paid_api_spent_usd": 0, "blockers": [],
   "rejected": ["H1 <short name> (holdout net -4.0 bps/trade)"],
   "evaluator": {"pack_sha256": "…", "pack_verify": true,
                 "reconciliations": ["R001 own engine vs official: costs within 1 bps/trade"]},
   "notes": []}
  ```

- **`PREREG/<id>.md`:** see references/research-cycle.md section 4 and `assets/team/authors/_template/PREREG_TEMPLATE.md`.
- **`TRIALS.jsonl`:** one JSON object per line, never edited; corrections are new lines. Kinds: `init`, `prereg`, `config`, `control`, `descriptive`, `exploration`, `code_check`, `data_check`, `reconciliation`, `correction`, `design_error`, `verdict`.

  ```json
  {"id":"P007","kind":"prereg","at_utc":"2025-03-14T14:59:01Z","prereg":"PREREG/P007_breakout.md","prereg_sha256":"<sha256>","decision":"D058","configs":["cfg-01","…","cfg-24"],"engine":"c2/engine.py","engine_sha256":"<sha256>","code":"c2/run.py","code_sha256":"<sha256>","seen_before":"no runs of this family; tail not opened"}
  {"id":"P007/cfg-01","kind":"config","at_utc":"2025-03-14T15:13:08Z","period":"DEV to the selection end","prereg_sha256":"<sha256>","n":40,"net":12.0,"stress":10.5,"qualified":false,"failed":["years_positive_ge60pct"]}
  {"id":"V-P007-final","kind":"verdict","at_utc":"2025-03-14T15:20:41Z","candidate":"cfg-07","look_at_utc":"2025-03-14T15:20:02Z","all":{"trades":50,"net":30.0},"tail":{"trades":10,"net":-1.0},"result":"no candidate: tail net < 0","final_sha256":"<sha256>"}
  ```

- **`candidates/<id>/`:** `recipe.json` with a passport (author, kind, model, submission time from the clock) and `REPORT.md` (hypothesis, gate K table, risks, number of trials, links to the PREREG and the trials).
- **`<cycle>/RESULTS.md`:** header (clock time; PREREG commit and time, before the runs; run times; label; where the raw outputs live, with their sha256 in TRIALS); body (summary, a table of all configs, long and short separately, controls, the one look, gates, conclusions); next step (options for the lead with a recommendation); a closing **honest label** (what was seen, what is indicative, what was computed after the runs and did not affect the selection).
- **`team/exchange/<name>.md`:** dated findings with evidence (paths, commands, hashes): data quirks, cost facts, regime facts, tool bugs, negative results.

### Judge files

- **`GATE_D_PROCEDURE.md`:** a version line with a change log naming decisions; thresholds; the sample; the submission tuple and the worse rule; the exact first-N recipe; the live part; reporting; known tool deviations; clones; calendar; active submissions.
- **`VERDICTS.jsonl`:** one line per (re)evaluation; a reissue adds a line with `supersedes`; old lines are never edited.

  ```json
  {"at_utc":"…","procedure":"GATE_D_PROCEDURE.md v1.2","kind":"monthly re-evaluation","judge":"judge","candidate":"B-03","author":"beta",
   "submission":{"file":"…/GATE_D_SUBMISSION.md","author_commit":"…","coord_merge_commit":"…","coord_merge_time":"…"},
   "submission_tuple":{"recipe_sha256":"…","declared_n":30,"execution_model_id":"…","evaluator_sha256":"…","code_sha256":"…"},
   "sample_start_utc":"…","sample_data_files_sha256":{"…":"…"},
   "verdict":"INSUFFICIENT","aggregates":{"closed_trades":1,"net":0.4,"stress":0.3,"max_drawdown":0.2,"largest_trade_share":1.0}}
  ```

- **`SEALED_1.json`, `FORWARD.json`:** bounds and hashes of every data load. **`LIVE_ROUNDS.jsonl`:** facts per live run for the live part of gate D.

### Review files

- **Plan check:** `# Fact check of plan <REL> · reviewer · <time>`; the plan file and commit, the `main` commit the line numbers refer to, the decisions it relies on, how it was checked; conclusion (blockers, numbered mandatory amendments, which need the lead's word); a table of plan claim → evidence (path:line).
- **Release review:** `# Review <REL> RC<n> (<scope>) · reviewer · <time>`; what was reviewed (branch, commits, base, hashes recomputed on a `git archive` copy); how (own probes, fresh-context lenses, documentation, what was not run and why); the verdict; what is confirmed (claim | yes/no | evidence); findings (ID | severity and area | what | evidence | proposal); what to do in the next RC; limitations; reproduction (scratch paths, probes, mutants).
- **Run review:** the same shape for one run; a quiet run gets a short helper receipt instead.
- **Candidate reproduction** (coordinator): who ran it, the exact command, versions and hashes, the numbers against the author's report, the clone check, the result.

### Platform files

- **`BACKLOG.md`:** ordered by priority; the lead settles disputes.
- **`RELEASES.md`:** one section per release, newest first: `## <REL> · <date> · <scope> (decisions) — <status> (review file; merge commit; code commit; kit sha)`; the answer table to the previous RC's findings; what the owner must know before installing; known limitations; core hashes; what changed; tests (counts, full suite on a clean snapshot, mutants, golden tests, guard tests, secret scan); the kit table (file | what | sha256). Statuses: RELEASE CANDIDATE, ACCEPTED, ACCEPTED WITH FINDINGS, NOT ACCEPTED, WITHDRAWN.

## 4. Communication

### Channels

Each role writes only `team/channel/<role>.md`, appending at the end; a correction is a new entry. Header `### <YYYY-MM-DD HH:MM> <TZ> · <from> → <to>[, <to2>] · <TYPE>`, then 1–8 lines (decisions may be longer). Types: INFO, ASK / ANSWER, CLAIM, DONE (with the commit), BLOCKED, REQUEST, RESULT (with the report path), PROPOSAL, ACK, DECISION (lead only). Examples are in `assets/team/channel/TEMPLATE.md`.

### Clock times

Read the clock in the same command that writes the entry; never type an estimated time; never copy a date from another file's header. Machine records (TRIALS, VERDICTS, STATUS) use UTC ISO times from the clock. The authoritative order of events is the coordinator's merge commits. In the reference team, estimated or copied timestamps forced append-only corrections and once a recipe rehash.

### Decisions

- Only the lead writes DECISION entries, in `team/channel/lead.md`. One global sequence `D001, D002, …`, never reused. A new team continues an existing project ledger instead of restarting the numbers. An addendum is a new entry (`D057 p. 5 (addendum)`); an owner clarification may take a letter (`D057a`). Cite by number and point.
- Shape: recipients `all` or a list of roles; first line `Dnnn — <one-line outcome>` naming the trigger and the evidence; numbered points with bold labels; a closing **Next** list.
- Owner quotes are verbatim, in quotation marks, with the place and the clock time; the quote is the authority.
- The coordinator copies each decision as one line into the decision ledger.
- A number copied from a review into a decision was once wrong when checked against the code; the decision had to be corrected. Verify numbers at the source before they become rules.

### Fast signals

A short session message points to the channel entry; group messages. Address sessions by session id or exact title. A message never overrides a file. Consent for an owner-only step counts only when the owner types it in the acting role's chat.

### Escalation, reports, idle

- Author or platform → coordinator → lead → owner; only strategic issues reach the lead (scope, gates and protocol, budgets, owner questions, conflicts).
- Authors: `STATUS.json` always current and ≤ 8 lines daily or at milestones; coordinator: ≤ 15 lines daily; reviewer: ≤ 15 lines per verdict with the file link; operator: ≤ 10 lines per operation or run.
- Idle rule: first useful preparation for blocked lines; then a genuinely new direction with a strict PREREG; otherwise end the turn and wait to be woken.

## 5. Git

- Trunk `main`; only the coordinator commits to it and pushes it. The lead stops committing once the team starts.
- A branch and a worktree per role, created from `main`, with an ignored role marker (`.team-role`) the guard reads. Merge `main` into your branch regularly, always after a release, and before PREREGs and paid calls.
- Path ownership: coordinator — merges, shared `team/*`, project memory, the lead's files; platform — core, `tools/`, `tests/`, web UI, `deployment/`, `team/platform/**`; author — `team/authors/<name>/**`, `team/exchange/<name>.md`; judge — `team/judge/**`; reviewer — `team/reviews/**`; operator — `team/ops/**`; everyone — their own channel.
- Integration push (coordinator): secret scan → `git push origin main` → `git push origin --all` → `git push origin --tags` → `git ls-remote origin` compared with local branches. Everyone else: `git push -u origin HEAD`.
- `git add` with explicit paths only. Commit messages in English, imperative, ≤ 72 characters in the first line.
- Never: force-push in any form; `--mirror`, `--prune`; rebasing pushed branches; history rewriting; `reset --hard`; `clean -f`; changing the remote, git config or hooks (owner only); deleting remote branches (owner only); deleting local branches or worktrees (coordinator only, on a lead decision); committing secrets, account data, `runtime/`, logs, large data or archives.
- If pushes are pre-approved in the client's permission settings (the reference team allowed only the plain forms `git push origin …` and `git push -u origin …`), the guard's push checks are what keep that safe. Never pre-approve a command family the guard does not fully parse.

## 6. The release flow

1. **Plan:** the platform writes `team/platform/<REL>_PLAN.md`, citing code lines.
2. **Fact check:** the reviewer checks the plan against the code: blockers and mandatory amendments.
3. **Lead DECISION:** answers to the amendments and the acceptance criterion, for example: no high or medium findings in the money path (orders, positions, protection, accounting, recovery); every planned item done or explicitly deferred; old exports byte for byte the same; full suites pass.
4. **Code** on the platform branch with tests and mutants; release notes; a kit with a sha256 table (RC1).
5. **In parallel:** the coordinator checks the kit hashes and runs the full suites on a clean snapshot of the exact RC commit, one suite at a time, zero errors (memory errors included; rerunning one module alone is never a pass of the suite); the reviewer reviews the RC.
6. **Verdict:** REJECT → the platform answers every finding in RC2 (finding → what was done) and a short RC2 review follows; ACCEPT or ACCEPT WITH FINDINGS → the coordinator merges with `--no-ff`, up to the tested commit only.
7. **Install window:** the lead approves it; the owner consents in the operator's chat, naming the kit sha256 and the steps.
8. **Install:** the operator installs between runs: backup → install → READY → receipt.
9. **First run:** the owner starts it; the reviewer reviews the first run on each new release.

Changes to the evaluator also need the judge's review. A core-hash change triggers requalification of frozen candidates and a new evaluation pack for all authors. A merged and verified kit stays as it is: rebuilding one with unreviewed code once disrupted an install the owner was waiting for. New code goes into a new candidate.

## 7. Live or production runs

1. The lead and the owner plan the run.
2. The operator checks the machine and writes the start card.
3. The owner presses Start.
4. The operator checks read-only after warm-up (for example at 35 minutes) and during the first two hours.
5. Evidence pack within about an hour of the end; the coordinator verifies the hashes.
6. A quiet run closes with a helper receipt; a run with a fill, an anomaly, a pause or an incomplete journal, and the first run on a new release, go to the reviewer.
7. The judge decides whether the run counts toward gate D.

On a pause or an anomaly, nobody but the owner presses anything; the lead says what the owner should do next. Supervision from chat sessions failed repeatedly in the reference team (references/lessons.md); put alerts in a plain program and make sure a human reacts.

## 8. Resources

- One heavy run at a time, team-wide: an OS lease plus a booking in BOARD through the coordinator. On BUSY, record it and try later; never retry in a loop.
- No heavy jobs during remote desktop work; few screenshots.
- Read data only from the canonical stores (absolute paths to the main checkout's ignored stores); write outputs to an ignored `runtime/team/<role>/`. Only summaries, recipes and code go into git.
- Full test suites only on a clean snapshot (`git archive <commit> | tar -x` in a scratch folder). A full run inside another role's live worktree failed with dozens of errors because that role edited files mid-run.

## 9. Checklists

- **Session start:** README → CHARTER → PROTOCOL → own brief → BOARD and OWNER_ACTIONS; `git merge main`; check the clock; a CLAIM or INFO in your channel.
- **Before a run:** PREREG and TRIALS line committed; configuration budget left; development data only; heavy run booked; no remote desktop session in progress.
- **Before a merge (coordinator):** allowed paths only; secret scan passes; suites on a clean snapshot of the exact commit; release notes and review verdict present.
- **Before asking the owner:** a concrete scenario, the recommended option first, two or three options, batched; record the answer verbatim with its time.
- **After an app restart:** confirm workspace trust for every worktree folder; check that each session answers, and its model and effort; re-read BOARD; resume from files, not from memory.
