# Research team: start here

Created with the ai-research-team skill on {{TODAY}}. The lead owns this file; the coordinator commits it.

**Mission of this stage:** <ONE OR TWO SENTENCES: the question the team must answer, and what counts as an answer, including "no".>

**Owner:** <OWNER NAME OR HANDLE> (human). The owner alone does anything with real-world consequences: money, accounts, keys, starting or stopping runs, production machines, publication.

## Who is who

| Role | Session title (the address for messages) | Model and effort | Workspace | Writes |
|---|---|---|---|---|
{{ROLE_ROWS}}

Short role names do not route messages between sessions: use the exact session title or session id. The coordinator keeps this table current in BOARD.md.

## Reading order

Every session, at every start and after every restart:

1. this file;
2. [CHARTER.md](CHARTER.md): owner decisions, statuses and gates, data partitions, limits;
3. [PROTOCOL.md](PROTOCOL.md): git, channels, decisions, resources, helpers, the guard;
4. your brief in [briefs/](briefs/);
5. [BOARD.md](BOARD.md) and [OWNER_ACTIONS.md](OWNER_ACTIONS.md);
6. the newest entries of your own channel and of `channel/lead.md`.

Then merge `main` into your branch, read the clock, and write a CLAIM or INFO entry in your channel.

## Absolute limits

1. Only the owner moves money, changes accounts or keys, starts, stops or resumes runs, installs on production machines, sets paid-API caps, administers git remotes and publishes. Agents prepare; the owner presses.
2. Files in git are the record. A chat message is only a signal that points to a file entry. When a message and a file disagree, the file wins.
3. Register before you test: the PREREG and its TRIALS line are committed before any run. Every attempt is logged, failed and interrupted ones included.
4. Research uses development data only. Held-out data stays with the judge.
5. Times come from the system clock, read in the same command that writes them.
6. Never open or print secret files. Never work around the guard: write BLOCKED to the coordinator instead.
7. Nothing is called profitable, proven or working without passing the gates. "No candidate" is a valid result.

## Layout

```
team/
  README.md  CHARTER.md  PROTOCOL.md  BOARD.md  OWNER_ACTIONS.md
  briefs/<role>.md        role briefs (lead)
  channel/<role>.md       append-only, one file per role; TEMPLATE.md shows the entry format
  exchange/<author>.md    verified findings, readable by everyone
  authors/<name>/         NOTEBOOK.md  STATUS.json  TRIALS.jsonl  PREREG/  candidates/  <cycle>/RESULTS.md
  authors/_template/      templates for new authors and pre-registrations
  platform/               BACKLOG.md  RELEASES.md  DATA.md  <REL>_PLAN.md (platform)
  judge/                  GATE_D_PROCEDURE.md  VERDICTS.jsonl (judge)
  reviews/                plan checks, release and run reviews, reproductions (reviewer, coordinator)
  ops/                    RUNBOOK.md  MACHINE.md  ROUNDS.md  START_CARD_<run>.md (operator)
  tools/                  guard.cjs  guard.config.json (lead only)
```

Data stores, the sealed vault, `runtime/` outputs and logs stay outside git.
