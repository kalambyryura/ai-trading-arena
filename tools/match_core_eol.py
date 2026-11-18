"""Give this worktree's trading_lab/ files the primary checkout's exact bytes where they differ only by line endings.

Why: the core source hashes (training, arena, exchange) read raw bytes. The primary checkout keeps some working
copies with CRLF while a fresh worktree gets LF from .gitattributes, so identical code hashes differently and
tools/strategy_round.py verify rejects the equal packet inside a worktree (engine_changed_reissue_equal_packet).

Content never changes: a file is copied only when both copies are equal after CRLF -> LF normalisation, so Git
sees no difference (eol=lf normalises on add). A file whose content really differs is reported and left alone
(merge main first). Do not run it while an evaluation in this worktree is running: the engine refuses to continue
when its loaded source hash changes.

  py -3.12 -X utf8 -B tools/match_core_eol.py            # dry run: what would change, hashes before/after
  py -3.12 -X utf8 -B tools/match_core_eol.py --apply
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from research_limits import shared_repository_root  # noqa: E402


def normalised(data):
    return data.replace(b"\r\n", b"\n")


def plan(worktree, primary):
    """Per-file decision for every trading_lab/*.py of the worktree."""
    rows = []
    for mine in sorted((Path(worktree) / "trading_lab").rglob("*.py")):
        rel = mine.relative_to(worktree).as_posix()
        theirs = Path(primary) / rel
        if not theirs.is_file():
            rows.append({"file": rel, "action": "skip_missing_in_primary"})
            continue
        a, b = mine.read_bytes(), theirs.read_bytes()
        if a == b:
            continue
        rows.append({"file": rel, "action": "copy_primary_bytes" if normalised(a) == normalised(b) else "skip_content_differs"})
    return rows


def core_hashes(root):
    """The three core source hashes computed over the files under root (same file lists as the engine)."""
    sys.path.insert(0, str(root))
    try:
        from trading_lab import arena, exchange_lab, strategy_training
        out = {}
        for name, module, function in (("training", strategy_training, "source_hash"), ("exchange", exchange_lab, "source_hash"),
                                       ("arena", arena, "arena_source_hash")):
            hasher = getattr(module, function)
            if hasattr(module, "SOURCE_FILES"):  # D098 engine: hashes take a folder and ignore line endings
                try:
                    out[name] = hasher(Path(root) / "trading_lab")
                except FileNotFoundError:  # that checkout runs an older engine file list
                    out[name] = None
                continue
            files = hasher.__code__.co_consts
            listed = next(c for c in files if isinstance(c, tuple) and c and all(isinstance(x, str) for x in c))
            out[name] = hashlib.sha256(b"".join((Path(root) / "trading_lab" / f).read_bytes() for f in listed)).hexdigest()
        return out
    finally:
        sys.path.remove(str(root))


def git(worktree, *args):
    return subprocess.run(["git", "-C", str(worktree), *args], capture_output=True, text=True)


def apply(worktree, primary, rows, stage=True):
    """Copy the primary bytes; with stage=True only files without uncommitted edits, then refresh their index entry.

    Git keeps listing a CRLF copy as modified until `git add` records its stat; the staged blob is the LF
    normalisation, i.e. exactly the committed content, so nothing new is staged and `git status` stays clean.
    """
    copied = []
    for row in rows:
        if row["action"] != "copy_primary_bytes":
            continue
        if stage and git(worktree, "diff", "--quiet", "--", row["file"]).returncode != 0:
            row["action"] = "skip_uncommitted_changes"
            continue
        target = Path(worktree) / row["file"]
        temporary = target.with_name(target.name + ".eol.tmp")
        temporary.write_bytes((Path(primary) / row["file"]).read_bytes())
        os.replace(temporary, target)
        copied.append(row["file"])
    if stage and copied:
        git(worktree, "add", "--", *copied)
        if git(worktree, "diff", "--cached", "--quiet", "--", *copied).returncode != 0:
            raise SystemExit("unexpected staged content after the line-ending copy; inspect `git diff --cached`")
    return copied


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--worktree", type=Path, default=HERE.parent)
    args = parser.parse_args(argv)
    worktree = args.worktree.resolve()
    primary = shared_repository_root(worktree)
    if primary == worktree:
        print(json.dumps({"primary_checkout": True, "changed": 0, "note": "nothing to do in the primary checkout"}))
        return 0
    rows = plan(worktree, primary)
    before = core_hashes(worktree)
    if args.apply:
        apply(worktree, primary, rows)
    after = core_hashes(worktree) if args.apply else None
    print(json.dumps({"worktree": str(worktree), "primary": str(primary), "applied": args.apply, "files": rows,
                      "hashes_before": before, "hashes_after": after, "primary_hashes": core_hashes(primary)}, indent=1))
    return 1 if any(r["action"] == "skip_content_differs" for r in rows) else 0


if __name__ == "__main__":
    raise SystemExit(main())
