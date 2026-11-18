# Safety rails

## Contents

1. Principles
2. Owner-only actions
3. The guard hook
4. Wiring the hook
5. Paid model calls: a budget with a local ledger
6. Other tooling
7. Assumptions to verify in your client

## 1. Principles

1. **Layers, not one wall.** The guard hook catches honest mistakes and casual prompt injection. It is a tripwire, not a sandbox. The hard limits sit outside the agents:
   - money: least-privilege API keys (read-only for readers; trade-only and never withdraw for executors the owner runs), a provider-side credit limit on every paid key, a separate account or subaccount per bot;
   - code: branch protection on the remote (no force-push on the default branch) and one integrator who reviews every merge;
   - people: the owner does every real-money action personally.
2. **The team prepares, the owner executes.** Agents write executors and test them with fake transports and temporary folders. Only the owner starts, stops or resumes anything that can trade or spend a shared budget.
3. **Models never own risk.** Deterministic code checks every model decision. An error, a timeout or an invalid answer means HOLD and no new entry.
4. **Evidence is append-only.** Nobody resets, deletes or rewrites ledgers, trial logs or journals. A new budget gets a new ledger.
5. **No routing around a refusal.** A blocked call is reported as BLOCKED with the exact command. Nobody rewords it, moves it to another tool or restarts a service to get past it. In the reference team the guard stopped an author from reading the file that enables paid calls, and the client's own classifier stopped the coordinator's first push to GitHub; in both cases the session asked for a rule instead of retrying in another form.
6. **Only code reads secrets.** Agents never open secret files; keys never appear in output, logs or commits.

## 2. Owner-only actions

| # | Action | Why only the owner |
|---|---|---|
| 1 | Placing, changing or cancelling orders; opening or closing positions on real accounts; changing leverage or margin mode | Irreversible financial effect; the owner bears the loss and the legal responsibility |
| 2 | Start, Stop and Resume of any run that can trade or spend a shared budget (live runs; in the strict variant demo and paper runs too) | A run commits money or budget; Stop closes positions, so it is a financial action; one person must own the state of every run |
| 3 | Transfers, deposits, withdrawals, wallet signatures, moving funds between accounts | Irreversible, and never needed for research |
| 4 | Creating or rotating API keys; scopes and IP allowlists; subaccounts; account settings; putting keys into env files | Least privilege holds only if the person who issues the keys sets it |
| 5 | Running live entrypoints and owner launchers; installing on production; importing or exporting runtime state; restarting the production server | These can cut off a running worker that holds open positions |
| 6 | Setting the paid-API budget and the provider credit limit; raising a cap; opening a new budget ledger | Real money, and the caps are part of the record |
| 7 | Resetting, deleting or editing a ledger, journal or trial log | Nobody does this, not even the owner for convenience: spend and trial counts are evidence |
| 8 | Git remote administration: remotes, credentials, branch protection, repository visibility, any force-push or history rewrite | Irreversible for collaborators; publication is the owner's decision |
| 9 | Relaxing an owner-only guard rule, or changing the hook wiring | The guard is the tripwire for everything above |
| 10 | Publishing results, a site or posts; sharing data; accepting terms; creating accounts; paying for services | Legal and reputational effect |
| 11 | Driving the owner's real browser, or a remote desktop to a machine that trades | At most one designated operator role does it, and never clicks real-money controls |
| 12 | Releasing sealed holdout data | Holdout integrity; only the judge and the platform handle it |

The team may write code and tests with fake transports and temporary folders; run GET-only checks with read-only keys that code reads, on allowlisted endpoints; do offline analysis and free rule-based simulations; make paid research calls only through the harness within the ledger; fetch public, keyless data through the fetch tool. Always: never open secret files; never commit secrets, account ids, raw account data, `runtime/`, logs or caches; give no financial advice; never present a plan, a demo, one round or an in-sample backtest as proof; label every claim with its evidence level.

## 3. The guard hook

### Files and modes

| File | Purpose | Edited by |
|---|---|---|
| `team/tools/guard.cjs` | the hook (Node 18+, no dependencies; from `assets/hooks/guard.cjs`) | lead only |
| `team/tools/guard.config.json` | paths, rules, profiles, roles (from `assets/hooks/guard.config.json`) | lead only; relaxing an owner-only rule needs the owner's yes |
| `.team-role` | untracked marker at the root of each role worktree; first line is the role name (UTF-8 or UTF-16) | lead, when creating the worktree |

```bash
node team/tools/guard.cjs                  # hook mode: reads one PreToolUse event from stdin
node team/tools/guard.cjs --explain < event.json   # prints {"decision","rule","reason","role","profile","tool"}
node team/tools/guard.cjs --check-config   # validates the config, lists unset paths, rules, profiles and roles
node team/tools/guard.cjs --self-test -v   # 116 built-in regression cases in a temp folder
```

Overrides for tests: `TEAM_GUARD_CONFIG` (config path), `TEAM_WORKTREES_ROOT`, `TEAM_GUARD_TMP`, `TEAM_GUARD_HOME`.

### Hook contract

The guard reads `tool_name`, `tool_input`, `cwd` and `transcript_path` from the event on stdin, and `CLAUDE_PROJECT_DIR` from the environment.

| Outcome | Exit | Output |
|---|---|---|
| allow | 0 | nothing; the normal permission rules still apply (the guard never prints `"permissionDecision":"allow"`, which would skip the user's prompts) |
| deny, `"output": "exit2"` (default) | 2 | stderr `team-guard blocked [<rule-id>]: <why>. <blockedSuffix>` |
| deny or ask, `"output": "json"` | 0 | stdout `{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny"\|"ask","permissionDecisionReason":"…"}}` |
| a rule with `"action": "ask"` in exit2 mode | 2 | treated as deny |
| stdin is not JSON | 0 | nothing |
| config missing or invalid | deny `config.invalid` | Read, Grep and Glob still pass, and so do edits of `guard.config.json` by unmarked sessions, so the lead can fix it |
| exception inside a check, `"onInternalError": "allow"` (default) | 1 | stderr `team-guard internal error (allowed): <name>`; the call goes ahead |
| exception, `"onInternalError": "deny"` | deny `guard.error` | |

Exit 2 is the default because every Claude Code version honours it and passes stderr back to the model. A hook timeout does not block, so input size is capped: commands over `maxCommandChars`, browser inputs over twice that in total, and code written by a restricted role that is over 20 times that or has a line longer than it are denied as `size.too-long` ("split it"). The built-in checks are linear or bounded. Keep your own patterns simple: a rule of the form `A[^\n]*B` costs about a second on a 100,000-character line with thousands of `A`s on a laptop. The optional `denialLog` (JSONL) records time, role, profile, tool, rule and the sha256 of the input, never the command text, which can hold secrets or private paths.

### Role resolution

1. **Transcript pin.** Claude Code stores a session's transcript under `~/.claude/projects/<start dir with every non-alphanumeric character replaced by ->/`. For each folder under `worktreesRoot` that holds a marker, the guard converts its path the same way and looks for it in `transcript_path`. A match fixes the role for the whole session, even after a `cd` into the main checkout. This check comes first because some desktop builds report the main checkout as `CLAUDE_PROJECT_DIR` in worktree sessions.
2. **Fallbacks:** walk up at most 8 folders from `CLAUDE_PROJECT_DIR`, then from the hook `cwd`, looking for the marker.
3. **No marker:** `unmarkedProfile` (the lead and the coordinator in the main checkout).
4. **A marker whose role is not in `roles`:** `unknownRoleProfile`, the most restricted profile.
5. **Hardening:** if the transcript points into `worktreesRoot` but no marker is found there (a renamed folder or a deleted marker), the session gets `unknownRoleProfile`, never the unmarked one. Marked sessions may not write any `.team-role`, may not run `git worktree` except `list` (gitRestricted profiles), and may not move or remove a worktree folder.

### Configuration reference

| Field | Meaning |
|---|---|
| `output` | `"exit2"` (default) or `"json"` |
| `onInternalError` | `"allow"` (default) or `"deny"` |
| `maxCommandChars`, `maxHeredocs` | size caps; beyond them `size.too-long` ("split it") |
| `caseInsensitivePaths` | `"auto"` (on for Windows and macOS), `true` or `false` |
| `roleMarker` | marker file name, default `.team-role` |
| `blockedSuffix` | text added to every denial, telling the agent what to do next |
| `denialLog` | optional JSONL path; leave it out to disable |
| `paths` | named locations, usable as `{tokens}`: `main` (the scaffold fills it; if unset, two folders above guard.cjs), `worktreesRoot`, `vault`, `dataStore`, `approvalFlag`, `ledgers`, any others; relative values are relative to `{main}`; a value like `<SEALED_HOLDOUT_DIR>` counts as unset and switches off the rules that use it. `codeDirs` and `codeExtensions` define `{code}` |
| `secretPaths` | `patterns` (regex sources, flag `i`) and `exceptions` (tested against the matched text, for example `.env.example`) |
| `git` | `pushRemotes` (the only allowed push targets), `protectedBranches` (pushed only by the integrator), `roleBranchPattern` (for example `team/{role}`: restricted roles push only HEAD or that branch) |
| `tools` | optional: `shell` (tool name → `["bash"]`, `["powershell"]` or both), `browser` and `realBrowser` (regexes over tool names) |
| `commandRules[]` | `id`; `pattern` (regex, flag `i`) and/or `script` (name or list: matches only when that script runs, never when it is read); `why`; `on` (any of `shell`, `browser`, `code`); `profiles` and `exceptProfiles` (`"*"`, `"@marked"`, `"@unmarked"` or profile names; a profile also matches rules naming a profile it extends); optional `unless` (a regex in which `{role}` becomes the session's role, for example "your own name as --spender"); optional `action` `"ask"` |
| `protectedPaths[]` | `id`, `paths`, `why`, `profiles` (default `["@marked"]`), `exceptProfiles`: any write naming these paths is denied (strict: a redirect, or any command naming them that is not a plain read) |
| `profiles.<name>` | `readDeny`, `writeAllow`, `writeDeny` (path lists), `allowAliasedPaths`, `gitRestricted`, `integrator`, `realBrowser`, `contentRules` (check code written by this role for secret mentions, `git push` and writes into the data store), `browserAllow` (regexes masked before browser rules apply, for an approved install), `extends` |
| `roles` | marker name → profile |
| `unmarkedProfile`, `unknownRoleProfile` | see role resolution |

**Path list entries:** an absolute path or one starting with a token (`{main}/runtime`, `{vault}`) covers that path and everything below it; a relative entry (`team/tools/guard.cjs`, `.claude/hooks`) matches that suffix anywhere, so every worktree's copy is covered. Special entries: `{worktree}` (the session's own worktree), `{otherWorktrees}` (every folder under `worktreesRoot` except the own one), `{code}` (the `codeDirs` and root-level code files of the main checkout and of every worktree), `{tmp}` (the OS temp folder, which holds the Claude scratchpad), `{home}`. Separators `\` and `/` are equal, `..` is resolved, relative paths resolve against the hook `cwd`, and `C:/x` matches the Git Bash form `/c/x`. Deny wins over allow. In `writeAllow`, an absolute entry outside the main checkout (for example `{tmp}` when the repository itself sits in the temp folder) never opens a path inside it.

### What the guard checks

**Shell commands** (Bash, PowerShell, terminal tools; a tool with an unknown shell is checked in both dialects):

1. Size cap.
2. Documentation is masked as data: heredoc bodies written to non-script files, PowerShell here-strings written with Set-Content or Out-File, comments, and git commit/merge/tag/stash/notes messages. Bodies fed to an interpreter or written to a script file stay, and so do `$( )` substitutions in unquoted heredocs.
3. The command is split into segments at unquoted newlines, `;`, `&` and `|`, quote- and escape-aware in both dialects.
4. If no segment runs code (an interpreter, `eval`, `source`, `xargs`, `curl`, `Invoke-Expression`, a script as the command word), search and print segments (`grep`, `rg`, `echo`, `Write-Output`, …) and read-only git are dropped from the active text, so text that only mentions a forbidden command is allowed.
5. `commandRules` with `on: shell` run on the active text.
6. Hidden commands: encoded PowerShell (`hidden.encoded-ps`), decode-and-run (`hidden.decode-run`), proxy overrides (`hidden.proxy`).
7. Git, parsed word by word, including quoted words that contain shell syntax (`bash -c "…"`, `pwsh -Command '…'`, `"$(…)"`), up to depth 3: push only to `pushRemotes`, with literal arguments, no force/mirror/delete/prune/receive-pack/exec/repo option or any unique prefix of one (`--forc`), no short cluster with `f` or `d`, no `+` or `:` refspec (`git.push-force`, `git.push-remote`, `git.push-literal`, `git.push-options`); restricted roles push only HEAD or their own branch, never a protected branch, `src:dst`, `--all`, `--tags` (`git.push-scope`); low-level transport (`git.lowlevel`); git through a variable, from code, through an alias or under xargs (`git.indirect`); `reset --hard`, `clean -f`, `checkout .` (`git.destructive`); `filter-branch`, `filter-repo`, `replace`, `fast-import`, reflog deletion, symbolic-ref changes (`git.history`); config writes and dangerous inline `-c` (`git.config`); remote changes (`git.remote`); `GIT_*` environment overrides such as `GIT_TRACE`, which can print tokens (`git.env`); credential helpers and stores (`git.credentials`); for restricted roles `update-ref`, branch deletion or moves, tag creation, `checkout -B`, `worktree` except list, fetch with refspecs (`git.refs`).
8. Writes: git internals and shell profiles for everyone (`git.files`); `protectedPaths` and role markers for marked roles (`protect.*`); worktree folder moves (`role.worktree-rename`); the profile's `writeDeny`, best effort: redirects, `sed -i`, write verbs, copy destinations, `git apply|am|checkout|restore|mv|rm`, output options such as `--out`, and inline code writes in Python, Node, pandas and .NET (`path.write-deny`). A path held in a variable escapes this check.
9. Secret files named by a non-listing segment (`secret.path`); listing (`ls`, `Test-Path`, `git status`, `git check-ignore`) may name them, and grep patterns and echo text are not operands.
10. Profile extras: `readDeny` paths in operands or pattern files (`path.read-deny`); links, junctions, `subst` and 8.3 short names for profiles with `allowAliasedPaths: false` (`path.aliased`).

**File tools** (Read, Edit, Write, MultiEdit, NotebookEdit, Grep, Glob): secret paths (Glob is listing and exempt; Grep's pattern is text); `readDeny`; writes against git internals, `protectedPaths`, markers, `writeDeny` and `writeAllow` (`path.write-allow`); code written into a file with a code extension against `on: code` rules and, for profiles with `contentRules`, the built-in content rules (`code.secret`, `code.git-push`, `code.data-write`).

**Browser tools:** a tool matching `realBrowser` (Claude in Chrome, computer use: they act with the owner's real sign-ins) is denied to marked roles without `realBrowser: true` (`role.real-browser`); every string in the input (URLs, typed text, scripts, form values) is checked for secret mentions (`secret.exfil`) and against `on: browser` rules.

**Any other matched tool** (for example Artifact, SendUserFile): every string in the input is checked for secret mentions (`secret.exfil`), so a secret file cannot leave through a publishing tool.

### What the guard cannot see

- Clicks by screen coordinates carry no text. The operator's rules cover them: read the label before clicking, stay on a read-only page when idle, never click real-money controls.
- Indirection: a path assembled in a variable, a `cd` earlier in the same command (relative paths resolve against the hook's `cwd`), a script written in one call and run later under a harmless name, downloaded code. Writes to script files are scanned as code, restricted roles cannot write code that names secrets, calls a paid provider directly or pushes, and one integrator reviews every diff; still, anything with an unrestricted shell can get around a text-based guard. That is why keys, credit limits, branch protection and the owner's hands are the real limits.
- False positives happen with text-based rules. Never weaken a built-in rail to fix one; add a narrower exception and a regression case. The reference guard grew from 49 regression cases on its first day to 339 at its fourth version.

### Changing the guard

- Only the lead edits `guard.cjs` and `guard.config.json`. A blocked role writes BLOCKED for the coordinator and sends the lead a REQUEST with the exact command and why it is legitimate, and works on something else meanwhile.
- After every change: `--check-config`, `--self-test`, and `--explain` on the new case (a BLOCK case and an ALLOW twin that uses the same words as data); the coordinator commits the files together.
- A change to `guard.cjs` or its config applies from the next tool call; a change to the hook wiring needs a session restart.
- Relaxing an owner-only rule needs the owner's explicit yes, recorded as a decision dated from the clock.

## 4. Wiring the hook

The scaffold merges `assets/hooks/settings.snippet.json` into the project's `.claude/settings.json`:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash|PowerShell|Read|Edit|Write|MultiEdit|NotebookEdit|Grep|Glob|WebFetch|Artifact|SendUserFile|mcp__terminal__.*|mcp__claude-in-chrome__.*|mcp__Claude_Browser__.*|mcp__computer-use__.*",
        "hooks": [{ "type": "command", "command": "node \"<MAIN_CHECKOUT>/team/tools/guard.cjs\"", "timeout": 15 }]
      }
    ]
  },
  "permissions": { "deny": ["Read(./.env)", "Read(./.env.local)", "Read(./.env.*.local)"] }
}
```

- **Matcher:** a regex over tool names; unmatched tools are not checked. List every tool that runs commands, reads or writes files, browses or fetches URLs, or publishes or sends content. Tool names differ between clients; MCP tools appear as `mcp__<server>__<tool>`.
- **Path:** the scaffold writes the absolute path of the main checkout, here and in `paths.main` of the guard config, so every worktree session runs the same reviewed guard and a stale or edited copy in a worktree cannot weaken it. Both files are tracked, so this puts your local folder name, often with your user name, into git. For a repository you will publish, scaffold with `--portable-hook` on the first run: the hook becomes `"$CLAUDE_PROJECT_DIR/team/tools/guard.cjs"` (each worktree then runs its own copy, which marked roles cannot edit) and `paths.main` becomes `{home}/…`. A rerun keeps existing files, so change an existing setup by hand.
- **Location:** the tracked `.claude/settings.json`, never only `.claude/settings.local.json`, which is per-machine and untracked, so role sessions would not get it. Only the owner commits changes to this wiring.
- **`permissions.deny`** is a second layer for the Read tool only; it does not stop `cat .env` in a shell, the guard does. Keep both.
- **Pre-approved pushes:** if the team pre-approves `git push origin …`, the guard's push checks are what keep that safe.
- **Restart** sessions after changing this block.

## 5. Paid model calls: a budget with a local ledger

1. **Owner:** sets the total budget and a hard credit limit on a dedicated provider key. That limit is the real money boundary.
2. **Harness** (one script, for example `tools/paid_research.py`; in the ai-trading-arena repository `tools/jev_research.py`): the only code path that makes paid calls. A ledger per spender with a cap, a reserve booked before each call, a block on unknown billing.
3. **Guard:** makes the harness the only way to a paid call (`paid.direct`, `paid.legacy`, `paid.spender`, `paid.none`, `paid.reconcile`, `paid.flag` in the default config).

Ledger rules that held up in the reference team:

- One database per spender under ignored `runtime/`, with an append-only journal next to it; statuses `reserved → accounted | invalid | over_reserve | billing_unknown`, and `reconciled` (coordinator only, booking at least the largest of reserve, reported and journal cost).
- Spend is the sum over accounted, invalid, over_reserve and reconciled calls, unrounded. A `billing_unknown` call adds nothing until reconciled but blocks every new call.
- The harness refuses when the database is missing while its journal exists (deleting the ledger must not reset the cap), when the stored cap differs from the code constant (editing a constant cannot raise it), when any call is open, when the database shows less spend than the journal, or when spend plus reserve would exceed the cap; it checks all of this in one exclusive transaction.
- It also refuses a body larger than the reserve was calibrated for, an unapproved request spec (the coordinator approves spec hashes after an independent review), a spender other than the caller's role, and a run over the per-run limit. No automatic retries.
- Valid answers are stored by request hash and replayed for free; invalid answers are never replayed. A dry run builds the exact request and prints its hashes without the network.
- Prompts are anonymised so the model cannot "remember" history: feature names from an allowlist with units, no digits, asset, venue or month names in question text, no absolute dates, times or prices. Experiments on news text run only forward in time, after the model's training cutoff.
- Reports show paid spend in USD separately from trading results; an equal-share split is labelled an estimate. A new cap means a new ledger file and an owner decision; old ledgers stay read-only as evidence.
- Calls made inside live or demo runs get their own ledger per run family, never shared with research: an attempt cap per run and per series, a soft USD cap with a reserve inside it, a cooldown, a block while any billing is unresolved. Recovery after a restart is close-only, with no paid calls.

The reference values: $1.00 cap per author, $0.01 reserve per call, at most 20 paid calls per run, requests of at most 8 kB. The first integration test allowed a single paid attempt and no retry.

## 6. Other tooling

- **Secret scan before every push:** collect exact secret values from the ignored env files (code reads them; they are never printed), match patterns (private-key blocks, bearer headers, provider key prefixes, env-style assignments of known key names whose values are not placeholders), scan every git object and the files a commit would add, print only the path and a label. Exit 1 on a finding. Run it on the whole history before a repository goes public. An off-the-shelf scanner such as gitleaks also works.
- **Heavy-run lease:** one heavy job at a time across all worktrees, through a non-blocking OS lock on a file in the main checkout's ignored `runtime/` (`msvcrt.locking` on Windows, `fcntl.flock` elsewhere), with a free-memory floor before extra slots. On BUSY, raise; nobody retries in a loop. The ai-trading-arena repository has a working version in `tools/research_limits.py`.
- **`.gitignore` baseline** (the scaffold adds the missing lines):

  ```gitignore
  .env
  .env.*
  !.env.example
  !.env.*.example
  .envrc
  local-secrets/
  *.private.zip
  runtime/
  logs/
  .team-role
  .claude/worktrees/
  .claude/settings.local.json
  ```

## 7. Assumptions to verify in your client

- **[ASSUMPTION] Transcript folder naming:** every non-alphanumeric character of the start directory becomes `-`. The role pin depends on it. Check once: start a session in a worktree and run `node team/tools/guard.cjs --explain` with a test event, or look at `~/.claude/projects/`.
- **[ASSUMPTION] Helper calls** go through the project PreToolUse hook with a transcript path under the caller's project folder. Check with a known BLOCK case run from a helper.
- **[ASSUMPTION] Non-blocking exits:** a hook timeout and exit codes other than 0 and 2 do not block the call.
- **[ASSUMPTION] `$CLAUDE_PROJECT_DIR`** expansion inside hook commands on Windows; use the absolute path (the scaffold default) if unsure.
- **Client-specific names:** tool names (`Artifact`, `SendUserFile`, browser and terminal MCP prefixes) differ between clients; list yours in the matcher and in `tools`.
- **Permission syntax** of `permissions.deny` may change between versions; check the current settings documentation.
