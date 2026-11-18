#!/usr/bin/env python3
"""Scaffold an AI research team (the ai-research-team skill) into a git repository.

Copies the team templates, the helper agents and the guard hook into the repository
without overwriting anything that already exists, wires the guard into
.claude/settings.json (idempotent; every other key is kept), adds missing .gitignore
lines and prints the next steps. Run it again at any time: a second run with the same
arguments changes nothing, and a run with more roles or authors adds only what is missing.

    python scaffold_team.py --repo PATH [--roles lead,coord,platform,author]
                            [--authors alpha,beta] [--dry-run] [--portable-hook]

Roles: lead, coord (coordinator), platform, author, judge, reviewer, ops (operator).
Standard library only; Python 3.10 or newer.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
ASSETS = SKILL_DIR / "assets"

ALL_ROLES = ("lead", "coord", "platform", "author", "judge", "reviewer", "ops")
DEFAULT_ROLES = ("lead", "coord", "platform", "author")
ROLE_ALIASES = {
    "lead": "lead", "coord": "coord", "coordinator": "coord", "platform": "platform", "engineer": "platform",
    "author": "author", "authors": "author", "judge": "judge", "reviewer": "reviewer", "ops": "ops", "operator": "ops",
}
BRIEFS = {
    "lead": "lead.md", "coord": "coordinator.md", "platform": "platform.md", "author": "author.md",
    "judge": "judge.md", "reviewer": "reviewer.md", "ops": "operator.md",
}
# Marker name -> guard profile. Authors map to "author"; lead and coord have no marker.
GUARD_PROFILES = {"platform": "engineer", "judge": "judge", "reviewer": "staff", "ops": "ops"}
WORKTREE_ROLES = ("platform", "judge", "reviewer", "ops")
ROLE_TABLE = {
    "lead": ("most capable, maximum effort", "main checkout, own files only", "charter, protocol, briefs, decisions, guard rules"),
    "coord": ("mid tier", "main checkout on `main`", "merges, BOARD, owner queue, project memory, reproductions"),
    "platform": ("most capable", "`.claude/worktrees/platform`, branch `team/platform`", "core code, tools, tests, data store, evaluator, releases"),
    "judge": ("most capable", "`.claude/worktrees/judge`, branch `team/judge`; vault outside the repo", "`team/judge/**`: procedure, verdicts"),
    "reviewer": ("most capable; maximum effort for money paths", "`.claude/worktrees/reviewer`, branch `team/reviewer`", "`team/reviews/**`"),
    "ops": ("most capable, high effort", "`.claude/worktrees/ops`, branch `team/ops`", "`team/ops/**`: runbook, rounds, start cards"),
}
AUTHOR_ROW = ("most capable, maximum effort", "`.claude/worktrees/{n}`, branch `team/{n}`", "`team/authors/{n}/**`, `team/exchange/{n}.md`")
AGENTS = ("helper-census.md", "helper-data.md", "helper-runner.md", "helper-scribe.md", "helper-verify.md")
AUTHOR_TEMPLATE = ("PREREG_TEMPLATE.md", "NOTEBOOK.md", "STATUS.json", "TRIALS.jsonl")
GITIGNORE_LINES = (
    ".env", ".env.*", "!.env.example", "!.env.*.example", ".envrc", "local-secrets/", "*.private.zip",
    "runtime/", "logs/", ".team-role", ".claude/worktrees/", ".claude/settings.local.json",
)
NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
RESERVED = set(ALL_ROLES) | {"coordinator", "operator", "engineer", "staff", "owner", "all", "_template"}


class ScaffoldError(Exception):
    pass


class Plan:
    """Collects every change; writes nothing in dry-run mode."""

    def __init__(self, repo: Path, dry_run: bool, roles: list[str], authors: list[str]):
        self.repo = repo
        self.dry_run = dry_run
        self.roles = roles
        self.authors = authors
        self.created: list[str] = []
        self.updated: list[str] = []
        self.kept: list[str] = []
        self.notes: list[str] = []

    def _write(self, rel: str, text: str) -> None:
        if self.dry_run:
            return
        path = self.repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")

    def create(self, rel: str, text: str) -> bool:
        if (self.repo / rel).exists():
            self.kept.append(rel)
            return False
        self._write(rel, text)
        self.created.append(rel)
        return True

    def update(self, rel: str, text: str, what: str) -> None:
        self._write(rel, text)
        self.updated.append(f"{rel} ({what})")


def read_asset(rel: str) -> str:
    path = ASSETS / rel
    if not path.is_file():
        raise ScaffoldError(f"missing skill asset: {path}")
    return path.read_text(encoding="utf-8")


def fill(text: str, values: dict[str, str]) -> str:
    for key, value in values.items():
        text = text.replace("{{" + key + "}}", value)
    return text


def parse_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


def parse_roles(raw: str | None, authors: list[str]) -> list[str]:
    names = parse_list(raw) or list(DEFAULT_ROLES)
    roles: list[str] = []
    for name in names:
        role = ROLE_ALIASES.get(name.lower())
        if role is None:
            raise ScaffoldError(f"unknown role {name!r}; choose from {', '.join(ALL_ROLES)}")
        if role not in roles:
            roles.append(role)
    if authors and "author" not in roles:
        roles.append("author")
    return [r for r in ALL_ROLES if r in roles]


def parse_authors(raw: str | None, roles_raw: str | None) -> list[str]:
    authors = []
    for name in parse_list(raw):
        low = name.lower()
        if not NAME_RE.match(low):
            raise ScaffoldError(f"author name {name!r}: use 1-32 lowercase letters, digits, '-' or '_', starting with a letter")
        if low in RESERVED:
            raise ScaffoldError(f"author name {name!r} is reserved for a role")
        if low not in authors:
            authors.append(low)
    if not authors and ("author" in [ROLE_ALIASES.get(r.lower()) for r in parse_list(roles_raw)] or not roles_raw):
        authors = ["alpha"]
    return authors


def role_rows(roles: list[str], authors: list[str]) -> str:
    rows = []
    for role in roles:
        if role == "author":
            for n in authors:
                model, place, writes = (part.format(n=n) for part in AUTHOR_ROW)
                rows.append(f"| author {n} | <session title> | {model} | {place} | {writes} |")
            continue
        model, place, writes = ROLE_TABLE[role]
        rows.append(f"| {role} | <session title> | {model} | {place} | {writes} |")
    rows.append("| helpers | (subagents) | smallest (Haiku) | the caller's workspace | only the output file of the job card |")
    return "\n".join(rows)


def board_rows(roles: list[str], authors: list[str]) -> str:
    rows = []
    for role in roles:
        if role == "author":
            rows.extend(f"| author {n} | <session title or id> | `team/{n}` / `.claude/worktrees/{n}` | <task> | <status> |" for n in authors)
        elif role in ("lead", "coord"):
            rows.append(f"| {role} | <session title or id> | main checkout | <task> | <status> |")
        else:
            rows.append(f"| {role} | <session title or id> | `team/{role}` / `.claude/worktrees/{role}` | <task> | <status> |")
    return "\n".join(rows)


def guard_roles(authors: list[str]) -> dict[str, str]:
    roles = dict(GUARD_PROFILES)
    for name in authors:
        roles[name] = "author"
    return roles


ROLES_BLOCK_RE = re.compile(r'"roles"\s*:\s*\{[^{}]*\}')


def roles_block(roles: dict[str, str]) -> str:
    return '"roles": {\n' + ",\n".join(f"    {json.dumps(k)}: {json.dumps(v)}" for k, v in roles.items()) + "\n  }"


def new_guard_config(repo_posix: str, roles: dict[str, str]) -> str:
    text = read_asset("hooks/guard.config.json")
    text = text.replace('"<MAIN_CHECKOUT>"', json.dumps(repo_posix))
    text, count = ROLES_BLOCK_RE.subn(lambda _m: roles_block(roles), text, count=1)
    if count != 1:
        raise ScaffoldError("the guard config asset has no roles block")
    json.loads(text)  # the result must stay valid JSON
    return text


def merge_guard_roles(path: Path, roles: dict[str, str]) -> tuple[str, list[str]] | None:
    try:
        original = path.read_text(encoding="utf-8-sig")
        data = json.loads(original)
    except (OSError, ValueError) as exc:
        raise ScaffoldError(f"{path} is not valid JSON ({exc}); fix it, then run again") from exc
    current = data.setdefault("roles", {})
    if not isinstance(current, dict):
        raise ScaffoldError(f"{path}: roles is not an object")
    profiles = data.get("profiles", {})
    added = []
    for role, profile in roles.items():
        if role in current:
            continue
        if isinstance(profiles, dict) and profile not in profiles:
            continue
        current[role] = profile
        added.append(f"{role}={profile}")
    if not added:
        return None
    # Rewrite only the roles block, so the lead reviews a diff of the added lines; reformat the whole
    # file only when that block cannot be found or the result would not parse back to the same data.
    text, count = ROLES_BLOCK_RE.subn(lambda _m: roles_block(current), original, count=1)
    try:
        if count == 1 and json.loads(text) == data:
            return (text if text.endswith("\n") else text + "\n"), added
    except ValueError:
        pass
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n", added


def home_relative(repo: Path) -> str | None:
    """paths.main as {home}/<path> when the checkout is under the home folder, so no user name is tracked."""
    try:
        rel = repo.relative_to(Path.home().resolve())
    except (ValueError, RuntimeError):
        return None
    return "{home}/" + rel.as_posix() if rel.parts else None


def config_main(path: Path) -> str:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        value = data.get("paths", {}).get("main", "")
        return value if isinstance(value, str) else ""
    except (OSError, ValueError, AttributeError):
        return ""


def hook_snippet(repo_posix: str, portable: bool) -> dict:
    target = "$CLAUDE_PROJECT_DIR" if portable else repo_posix
    return json.loads(read_asset("hooks/settings.snippet.json").replace("<MAIN_CHECKOUT>", target))


def merge_settings(existing: dict, snippet: dict) -> tuple[dict, list[str], str | None]:
    data = json.loads(json.dumps(existing))
    changes: list[str] = []
    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ScaffoldError(".claude/settings.json: 'hooks' is not an object")
    pre = hooks.setdefault("PreToolUse", [])
    if not isinstance(pre, list):
        raise ScaffoldError(".claude/settings.json: 'hooks.PreToolUse' is not a list")
    wanted = snippet["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    found = [
        str(h.get("command", ""))
        for entry in pre if isinstance(entry, dict)
        for h in (entry.get("hooks") or []) if isinstance(h, dict)
        if "guard.cjs" in str(h.get("command", ""))
    ]
    note = None
    if not found:
        pre.extend(json.loads(json.dumps(snippet["hooks"]["PreToolUse"])))
        changes.append("guard hook")
    elif wanted not in found:
        note = f"a guard hook is already wired with a different command ({found[0]}); left unchanged"
    perms = data.setdefault("permissions", {})
    if not isinstance(perms, dict):
        raise ScaffoldError(".claude/settings.json: 'permissions' is not an object")
    deny = perms.setdefault("deny", [])
    if not isinstance(deny, list):
        raise ScaffoldError(".claude/settings.json: 'permissions.deny' is not a list")
    for rule in snippet["permissions"]["deny"]:
        if rule not in deny:
            deny.append(rule)
            changes.append(f"deny {rule}")
    return data, changes, note


def gitignore_update(path: Path) -> tuple[str, list[str]] | None:
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    present = {line.strip() for line in existing.splitlines()}
    missing = [line for line in GITIGNORE_LINES if line not in present]
    if not missing:
        return None
    text = existing
    if text and not text.endswith("\n"):
        text += "\n"
    if text:
        text += "\n"
    text += "# ai-research-team: secrets, runtime output, role markers and role worktrees stay out of git\n"
    text += "\n".join(missing) + "\n"
    return text, missing


def scaffold(args: argparse.Namespace) -> Plan:
    repo = Path(args.repo).expanduser().resolve()
    if not repo.is_dir():
        raise ScaffoldError(f"--repo {repo} is not a directory")
    authors = parse_authors(args.authors, args.roles)
    roles = parse_roles(args.roles, authors)
    if "author" not in roles:
        authors = []
    plan = Plan(repo, args.dry_run, roles, authors)
    if not (repo / ".git").exists():
        plan.notes.append(f"{repo} is not a git repository yet: run `git init` there before committing")

    now = dt.datetime.now(dt.timezone.utc)
    values = {
        "TODAY": dt.datetime.now().astimezone().strftime("%Y-%m-%d"),
        "NOW_UTC": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ROLE_ROWS": role_rows(roles, authors),
        "BOARD_ROLE_ROWS": board_rows(roles, authors),
    }
    repo_posix = repo.as_posix()

    # 1. Shared team files and channels.
    for name in ("README.md", "CHARTER.md", "PROTOCOL.md", "BOARD.md", "OWNER_ACTIONS.md"):
        plan.create(f"team/{name}", fill(read_asset(f"team/{name}"), values))
    plan.create("team/channel/TEMPLATE.md", read_asset("team/channel/TEMPLATE.md"))
    channel_owners = [r for r in roles if r != "author"] + authors
    for owner in channel_owners:
        plan.create(
            f"team/channel/{owner}.md",
            f"# Channel: {owner}\n\nAppend-only. Only {owner} writes here, and only at the end; a correction is a new entry.\n"
            "Entry format and types: [TEMPLATE.md](TEMPLATE.md). Read the clock in the same command that writes an entry.\n",
        )

    # 2. Role briefs.
    for role in roles:
        plan.create(f"team/briefs/{BRIEFS[role]}", read_asset(f"team/briefs/{BRIEFS[role]}"))

    # 3. Author folders: a template folder for later authors, then one folder per author.
    if authors:
        for name in AUTHOR_TEMPLATE:
            plan.create(f"team/authors/_template/{name}", read_asset(f"team/authors/_template/{name}"))
    for author in authors:
        author_values = dict(values, AUTHOR=author)
        for name in ("NOTEBOOK.md", "STATUS.json", "TRIALS.jsonl"):
            plan.create(f"team/authors/{author}/{name}", fill(read_asset(f"team/authors/_template/{name}"), author_values))
        plan.create(
            f"team/exchange/{author}.md",
            f"# Findings: {author}\n\nVerified findings for everyone: data quirks, cost facts, regime facts, tool bugs and negative results.\n"
            "One dated entry per finding, with its evidence (paths, commands, hashes). Append-only.\n",
        )

    # 4. Helper agents.
    for name in AGENTS:
        plan.create(f".claude/agents/{name}", read_asset(f"agents/{name}"))

    # 5. The guard and its config.
    plan.create("team/tools/guard.cjs", read_asset("hooks/guard.cjs"))
    wanted_roles = guard_roles(authors)
    config_rel = "team/tools/guard.config.json"
    main_value = (home_relative(repo) or repo_posix) if args.portable_hook else repo_posix
    if plan.create(config_rel, new_guard_config(main_value, wanted_roles)):
        if args.portable_hook and main_value == repo_posix:
            plan.notes.append("this checkout is not under your home folder, so paths.main in team/tools/guard.config.json"
                              " names its absolute path; edit it before publishing the repository")
    else:
        if args.portable_hook and re.match(r"^([A-Za-z]:[\\/]|/)", config_main(repo / config_rel)):
            plan.notes.append("team/tools/guard.config.json already exists and its paths.main is an absolute path (kept);"
                              " write it as {home}/... by hand before publishing the repository")
        merged = merge_guard_roles(repo / config_rel, wanted_roles)
        if merged:
            text, added = merged
            plan.update(config_rel, text, "roles " + ", ".join(added))

    # 6. Hook wiring in .claude/settings.json (tracked, so every worktree gets it).
    settings_rel = ".claude/settings.json"
    settings_path = repo / settings_rel
    existing: dict = {}
    if settings_path.exists():
        try:
            existing = json.loads(settings_path.read_text(encoding="utf-8-sig"))
        except ValueError as exc:
            raise ScaffoldError(f"{settings_path} is not valid JSON ({exc}); fix it, then run again") from exc
        if not isinstance(existing, dict):
            raise ScaffoldError(f"{settings_path} must hold a JSON object")
    merged_settings, changes, note = merge_settings(existing, hook_snippet(repo_posix, args.portable_hook))
    if note:
        plan.notes.append(note)
    if changes:
        text = json.dumps(merged_settings, indent=2, ensure_ascii=False) + "\n"
        if settings_path.exists():
            plan.update(settings_rel, text, ", ".join(changes))
        else:
            plan.create(settings_rel, text)
    else:
        plan.kept.append(settings_rel)

    # 7. .gitignore baseline.
    ignore = gitignore_update(repo / ".gitignore")
    if ignore:
        text, missing = ignore
        if (repo / ".gitignore").exists():
            plan.update(".gitignore", text, "added " + " ".join(missing))
        else:
            plan.create(".gitignore", text)
    else:
        plan.kept.append(".gitignore")
    return plan


def report(plan: Plan, args: argparse.Namespace) -> None:
    mode = "DRY RUN, nothing written" if plan.dry_run else "done"
    print(f"ai-research-team scaffold ({mode}) in {plan.repo}")
    print(f"roles: {', '.join(plan.roles)}" + (f"; authors: {', '.join(plan.authors)}" if plan.authors else ""))
    verb = "would create" if plan.dry_run else "created"
    print(f"{verb}: {len(plan.created)} file(s)")
    for rel in plan.created:
        print(f"  + {rel}")
    if plan.updated:
        print(f"{'would update' if plan.dry_run else 'updated'}: {len(plan.updated)} file(s)")
        for rel in plan.updated:
            print(f"  ~ {rel}")
    print(f"kept as they were: {len(plan.kept)} file(s)")
    if not plan.created and not plan.updated:
        print("nothing to change: the team is already scaffolded for these roles")
    for note in plan.notes:
        print(f"note: {note}")
    if not args.portable_hook:
        print("note: .claude/settings.json and paths.main in team/tools/guard.config.json name the absolute path of this"
              " checkout, so every worktree runs the same reviewed guard; both files are tracked and show your local folder"
              " name. For a repository you will publish, scaffold with --portable-hook on the first run (a rerun keeps"
              " existing files), or set the hook to node \"$CLAUDE_PROJECT_DIR/team/tools/guard.cjs\" and paths.main to"
              " {home}/... by hand.")

    marked_all = [r for r in plan.roles if r in WORKTREE_ROLES] + plan.authors
    existing = [r for r in marked_all if (plan.repo / ".claude" / "worktrees" / r).exists()]
    marked = [r for r in marked_all if r not in existing]
    print()
    print("Next steps")
    print("1. Interview the owner (concrete scenarios, recommended option first, batched) and replace every <...> in")
    print("   team/CHARTER.md: owner decisions G1..Gn, budgets, data cutoffs, gate thresholds. Then team/README.md.")
    print("2. Adapt team/tools/guard.config.json: set paths.vault and paths.dataStore, and list this project's live")
    print("   entrypoints, launchers and private endpoints in commandRules. Then check it:")
    print("     node team/tools/guard.cjs --check-config")
    print("     node team/tools/guard.cjs --self-test")
    print("3. Review and commit with explicit paths (never git add -A):")
    print("     git add team .claude/agents .claude/settings.json .gitignore")
    print('     git commit -m "Set up the research team"')
    if marked:
        print("4. Create a worktree, a branch and a role marker for each marked role (lead and coord stay here):")
        for role in marked:
            print(f"     git worktree add .claude/worktrees/{role} -b team/{role}")
            print(f"     echo {role} > .claude/worktrees/{role}/.team-role")
        print("   In Windows PowerShell write markers with: Set-Content -Encoding ascii <path>/.team-role <role>")
        if existing:
            print(f"   Worktrees that already exist stay as they are: {', '.join(existing)}")
    elif existing:
        print(f"4. Every marked role already has its worktree: {', '.join(existing)}")
    print("5. Restart Claude Code sessions so the hook loads (hooks are read at session start), confirm workspace")
    print("   trust for every worktree folder, and start one session per role in its folder with:")
    print('     "You are <role> of this research team. Read team/README.md, team/CHARTER.md, team/PROTOCOL.md and')
    print('      team/briefs/<brief>.md, then team/BOARD.md and team/OWNER_ACTIONS.md, and follow them."')
    print("6. Day 1: the platform builds the data store, the sealed vault (hashes only) and the evaluator. Authors")
    print("   write their day-1 notebook sections and run nothing until the evaluator exists.")


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    parser = argparse.ArgumentParser(description="Scaffold an AI research team into a git repository (ai-research-team skill).")
    parser.add_argument("--repo", required=True, help="root of the target git repository (the main checkout)")
    parser.add_argument("--roles", help=f"comma-separated roles (default: {','.join(DEFAULT_ROLES)}); all: {','.join(ALL_ROLES)}")
    parser.add_argument("--authors", help="comma-separated author names (default: alpha when the author role is chosen)")
    parser.add_argument("--dry-run", action="store_true", help="show what would change and write nothing")
    parser.add_argument("--portable-hook", action="store_true",
                        help="wire the hook as node \"$CLAUDE_PROJECT_DIR/team/tools/guard.cjs\" and write paths.main as"
                             " {home}/... instead of absolute paths (for a repository that will be published)")
    args = parser.parse_args(argv)
    try:
        plan = scaffold(args)
    except ScaffoldError as exc:
        print(f"scaffold_team: {exc}", file=sys.stderr)
        return 2
    report(plan, args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
