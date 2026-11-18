---
name: ai-research-team
description: Set up and run a disciplined research team of Claude Code sessions in one git repository - a lead who decides, a coordinator who alone merges, a platform engineer, competing authors who pre-register every test, a judge who alone holds unseen data, a reviewer, an operator and cheap Haiku helpers, with owner-only rules and a guard hook as a tripwire so that only the human owner moves money. Use this skill whenever someone wants to set up a multi-agent or multi-session research team, split a long research project across several Claude sessions with roles, start or run a research cycle with pre-registration and pass gates, write or onboard a role brief, continue the crypto trading-strategy research of the ai-trading-arena repository, or asks how that AI team worked and what it learned, even if they do not name the skill.
---

# AI research team

Set up and run a team of Claude Code sessions that researches through one git repository under written rules: a lead who decides, a coordinator who alone merges, a platform engineer who owns the shared tools, authors who compete on pre-registered hypotheses, a judge who alone holds the unseen data, a reviewer, an operator of the production machine, and cheap helper agents. Only the human owner presses buttons that move money.

The structure comes from a real project, the ai-trading-arena repository by kalambyrYura. From 28 Oct to 17 Nov 2026 eleven Claude Code sessions and five helpers researched crypto perpetual-futures strategies, with real money in the owner's live rounds. The process did what it was built for: independent reviews stopped faulty releases before installation, pre-registration kept every failed idea on the record, and every money action stayed with the owner. It found no strategy with an edge after costs. Expect the same honesty from your team: the structure makes research trustworthy, not successful.

## Decide first: a team, or one session

A team multiplies usage and coordination work. Use one only when most of these hold:

- several independent lines of research can run in parallel, and you want them to compete without copying each other;
- results must be checked by someone who did not produce them (authors and judge, platform and reviewer);
- the work touches real money, production or anything irreversible, so you need an audit trail and exactly one person who presses buttons;
- the project runs for weeks, so sessions will restart and the state must live in files.

Stay with one session plus helpers when the question fits in a day or two, there is one line of work, or nothing touches money or production. Keep the research discipline anyway (pre-register, log every trial, look at held-out data once; see references/research-cycle.md and references/gates.md): that discipline produced most of the value, and the roles mainly protect it.

Never start with the full cast. The reference team started six sessions on day 0, before the shared evaluator and the data store existed: the authors built private engines, filed overlapping requests and then sat idle. Start with the lead, the coordinator, the platform engineer and one or two authors. Add the judge when the first candidate approaches, the reviewer when releases touch money or production, the operator when an always-on production machine exists.

## Quick start

1. **Interview the owner before anything else.** The owner's answers become numbered decisions (G1, G2, …) in the charter: goal and what counts as an honest "no", scope, money at risk, paid-API budget and the provider credit limit, time box and stop rule, what the lead decides alone, live policy, publication rights, data cutoffs. Ask with a concrete scenario and a named example, recommended option first, two or three options, several questions per message. Abstract questions came back as "rephrase" in the reference project.
2. **Scaffold the files** with the script in this skill's own `scripts/` folder (Python 3.10+, standard library only; `<skill-dir>` is this skill's base directory, `/path/to/repo` the main checkout of the project). Show the owner the dry run first:

   ```bash
   python <skill-dir>/scripts/scaffold_team.py --repo /path/to/repo --roles lead,coord,platform,author --authors alpha,beta --dry-run
   python <skill-dir>/scripts/scaffold_team.py --repo /path/to/repo --roles lead,coord,platform,author --authors alpha,beta
   ```

   It copies `team/` (README, CHARTER, PROTOCOL, BOARD, OWNER_ACTIONS, channels, briefs, author folders), the five helper agents into `.claude/agents/`, and the guard into `team/tools/`; merges the hook into `.claude/settings.json` (other keys kept); adds missing `.gitignore` lines; and prints the next steps. It never overwrites a file, so you can rerun it to add roles or authors. By default the hook command and `paths.main` in the guard config hold the checkout's absolute path, which shows your local folder name in tracked files; for a repository you will publish, add `--portable-hook` on the first run (`$CLAUDE_PROJECT_DIR` in the hook, `{home}/…` in the config).
3. **Fill the placeholders** in `team/CHARTER.md` and `team/README.md`, and adapt `team/tools/guard.config.json`: the vault and data store paths, and every live entrypoint, launcher and private endpoint of this project. Then run `node team/tools/guard.cjs --check-config` and `node team/tools/guard.cjs --self-test`.
4. **Commit with explicit paths**, then create one worktree, branch and role marker per marked role, as the scaffold prints (`git worktree add .claude/worktrees/<role> -b team/<role>`, then the role name into `.team-role`). The lead and the coordinator stay in the main checkout without a marker.
5. **Restart Claude Code** so the hook loads, confirm workspace trust for every worktree folder, and start one session per role in its folder: "You are `<role>` of this research team. Read team/README.md, team/CHARTER.md, team/PROTOCOL.md and team/briefs/`<brief>`.md, then team/BOARD.md and team/OWNER_ACTIONS.md, and follow them." Give each session a distinctive title: titles are the message addresses.
6. **Day 1 belongs to the platform:** data store with manifests, sealed vault loaded by hashes only, official evaluator with tests. Authors write their notebook's day-1 hypotheses and run nothing until the evaluator exists.

When you are the session doing the setup, play the lead: interview the owner, write the decisions, and let the scaffold do the copying. Do not create worktrees, start sessions or wire hooks on another person's machine without their explicit go-ahead; hook wiring changes what every later session may do.

## How sessions talk

- **Files in git are the record; messages are signals.** Each role appends to its own `team/channel/<role>.md` with a header `### <date time TZ> · <from> → <to> · <TYPE>` and 1–8 lines. A short session message only points to the entry. Sessions restart, lose context and receive messages late; a file in git survives all of that, and a new session catches up with `git pull`.
- **Files win.** When a message and a file disagree, ask the lead for a new decision entry. In the reference team a DECISION and a later chat message named different roles for the same job.
- **Only the lead writes DECISION entries,** numbered `D001, D002, …` and never reused, with the owner's words quoted verbatim, with place and time, when they are the authority.
- **One integrator.** Only the coordinator commits to `main` and pushes it, after checking that a branch touches only its owner's paths and passes the secret scan, and that a release passes the full suites on a clean snapshot. Everyone else commits on its own branch.
- **Times come from the clock** (`date` in the same command that writes the entry). Eight of the eleven channels in the reference team needed corrections of estimated timestamps, some off by a whole day.
- **Address sessions by exact title or id.** Short role names did not route.
- **Consent for an owner-only step counts only when the owner types it in the chat of the role that will act.** A consent relayed by another session does not count.
- **Escalate** author or platform → coordinator → lead → owner; only strategic questions reach the lead. Authors send the coordinator at most 8 lines a day; the coordinator sends the lead at most 15.

## The research cycle

1. **The lead opens a cycle with a DECISION:** goal; principles mandatory for every candidate (cost model, minimum gross move per outcome against the round-trip cost, controls); a configuration budget per author (for example 24); one look at the held-out tail; directions per author; deadlines.
2. **Register before you test.** The author merges `main`, reads the clock, writes the PREREG (mechanism, what refutes it, exact definitions, the full grid, the selection rule, the one-look rule, controls), appends its line to `TRIALS.jsonl` with file hashes, commits both in their own commit, pushes, and only then runs. A PREREG registered 32 seconds before a run but committed after it proved nothing in the reference project.
3. **Run in two stages:** selection on the selection span with the pre-declared criteria (every configuration logged), then one look at the full development set and its tail. A failed look closes the hypothesis; it is never retuned on the tail.
4. **Run the controls:** long and short separately; random entries with the same sides and holding times; a shifted-feature placebo; neighbouring configurations. In the reference project random entries earned 462 bps per trade against 622 for a breakout rule: most of the "edge" was the market rising.
5. **Report honestly:** `RESULTS.md` with every configuration, the controls and a closing honest label (what was seen, what is indicative, what was computed after the runs). "No candidate" is a valid result.
6. **Candidate path:** gate K passed → the coordinator reproduces it with the official evaluator and checks for clones → CANDIDATE → the author submits a frozen hash and a declared N to the judge → the judge runs gate D on unseen data and reports PASS, FAIL or INSUFFICIENT with aggregates only.
7. **The lead closes the cycle** with a DECISION: a table of author, direction and result, lessons, next steps. A new cycle that changes scope or budget needs the owner's yes.

## Safety rails

- **Only the owner touches the real world:** orders and positions, Start/Stop/Resume of any run (Stop closes positions, so it moves money too), transfers, keys and account settings, installs and restarts on production, paid-API caps, git remotes and repository visibility, publication. Agents write and test executors with fake transports and temporary folders; the owner runs them.
- **Models never own risk.** Deterministic code checks every model output; an error, a timeout or an invalid answer means HOLD and no new action.
- **The guard hook is a tripwire, not a sandbox.** `team/tools/guard.cjs` runs before every shell, file, browser and publishing tool call and blocks, from `guard.config.json`: owner-only commands and routes, force pushes and history rewrites, git config and remote changes, secret files in any letter case, and role limits (sealed data, data downloads, paid calls outside the harness, code edits by reviewer and operator, pushes outside one's own branch). It reads the role from a `.team-role` marker pinned by the session's transcript folder, so a `cd` does not escape it. It cannot see clicks by screen coordinates, so the operator's click rules cover that gap.
- **Never route around a refusal.** A blocked call is reported as BLOCKED with the exact command; nobody rewords it, moves it to another tool or restarts a service to get past it. Only the lead edits the guard; relaxing an owner-only rule needs the owner's yes.
- **The real limits sit outside the agents:** least-privilege keys (read-only for readers, trade-only and never withdraw for executors the owner runs), a provider-side credit limit on every paid key, a separate account per bot, branch protection on the remote, and the owner's own hands.
- **Evidence is append-only:** ledgers, trial logs, journals and verdicts are never reset or edited. A new budget gets a new ledger.
- **Only code reads secrets.** No agent opens or prints `.env*`, key files or private settings.

## Costs and limits

- **Weekly usage limits ran out more than once in the reference team.** Around ten sessions, most on the strongest model at maximum effort, used up the weekly limit, and the whole team stood still for about three days. A 24-hour live round ran unsupervised after its first two hours, and its evidence was collected days later instead of within an hour of the end.
- Put the coordinator on a mid-tier model: it does many small steps all day. Give routine work (test runs, counts, claim checks, formatting) to the Haiku helpers with a job card. Keep the strongest model for authors, the judge, the reviewer and the lead.
- End the turn when there is no useful work (the idle rule); the coordinator wakes sessions on triggers. Never duplicate runs: CLAIM work in the channel first. Keep state in files so work resumes after a reset, and plan big work around the reset date.
- Saving limits must not cut quality: the owner asked the team never to slim down reviews or pause work to save limits. Efficiency means no wasted work.
- Check each session's model before expensive work: a session can silently run on a different model, for example after a safety fallback.
- Paid model calls in research go through a harness with a ledger per role and a hard cap (the reference team allowed $1.00 per author; the whole paid decision-model experiment cost $0.19 and added nothing measurable). The real costs were time and subscription limits.
- One heavy run at a time across the team, through an OS lease and a booking with the coordinator; no heavy jobs while someone drives a remote desktop (the reference laptop froze hard when both ran together; the cause was never proven).
- Do not leave live supervision to a chat session. Session timers failed repeatedly; a pause went unnoticed overnight. Use a plain program that alerts a human, and make sure a human reacts.

## Continue the ai-trading-arena research

To pick up the crypto strategy research of that repository, read references/continue-this-project.md first: the honest state (no edge found; one strategy passed the backtest gate with a thin margin and has 1 of 30 forward trades; a second, which failed the backtest gate on one check, started its forward test on 17 Nov 2026 with 0 of 30 trades), the untested ideas, where everything is in the repository, and how to run a new team there with the default guard config, which already blocks that repository's live entrypoints.

## Where to read more

- references/roles.md: every role's purpose, inputs, outputs, permissions and limits; helpers; when to add each role.
- references/protocol.md: git, channels, decisions, file formats (BOARD, owner queue, STATUS, TRIALS, verdicts, reviews), resources, checklists.
- references/research-cycle.md: data partitions, the cycle step by step, controls, research hygiene, the PREREG and RESULTS formats.
- references/gates.md: status ladder, gate K and gate D, declared N, the worse rule, adapted gates for rare trades, how to calibrate gates, how to adapt them to another domain.
- references/safety.md: owner-only actions, the guard (config, role resolution, rule ids, limits), hook wiring, the paid-API ledger, secret scan.
- references/lessons.md: what worked and what did not in the reference team, with numbers.
- references/continue-this-project.md: state of the ai-trading-arena research and how to continue it.
