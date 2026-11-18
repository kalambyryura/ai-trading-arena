"""EOL matcher: a worktree gets the primary checkout's bytes only when files differ by line endings alone."""
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import match_core_eol
from research_limits import shared_repository_root


def layout(root):
    primary, tree = root / "primary", root / "tree"
    gitdir = primary / ".git" / "worktrees" / "tree"
    gitdir.mkdir(parents=True)
    (gitdir / "commondir").write_text("../..\n", encoding="utf-8")
    (tree / "trading_lab").mkdir(parents=True)
    (primary / "trading_lab").mkdir(parents=True)
    (tree / ".git").write_text("gitdir: " + str(gitdir) + "\n", encoding="utf-8")
    return primary, tree


class MatchCoreEolTests(unittest.TestCase):
    def test_only_line_ending_differences_are_copied(self):
        with tempfile.TemporaryDirectory() as folder:
            primary, tree = layout(Path(folder))
            self.assertEqual(shared_repository_root(tree), primary.resolve())
            (primary / "trading_lab" / "a.py").write_bytes(b"x = 1\r\ny = 2\r\n")
            (tree / "trading_lab" / "a.py").write_bytes(b"x = 1\ny = 2\n")
            (primary / "trading_lab" / "b.py").write_bytes(b"x = 1\r\n")
            (tree / "trading_lab" / "b.py").write_bytes(b"x = 2\n")
            (primary / "trading_lab" / "c.py").write_bytes(b"same\n")
            (tree / "trading_lab" / "c.py").write_bytes(b"same\n")
            (tree / "trading_lab" / "d.py").write_bytes(b"new\n")
            rows = match_core_eol.plan(tree, primary)
            self.assertEqual({r["file"]: r["action"] for r in rows},
                             {"trading_lab/a.py": "copy_primary_bytes", "trading_lab/b.py": "skip_content_differs",
                              "trading_lab/d.py": "skip_missing_in_primary"})
            self.assertEqual(match_core_eol.apply(tree, primary, rows, stage=False), ["trading_lab/a.py"])
            self.assertEqual((tree / "trading_lab" / "a.py").read_bytes(), b"x = 1\r\ny = 2\r\n")
            self.assertEqual((tree / "trading_lab" / "b.py").read_bytes(), b"x = 2\n", "real content changes are never copied")
            self.assertEqual(match_core_eol.plan(tree, primary)[0]["file"], "trading_lab/b.py")

    def test_core_hashes_use_the_engine_file_lists(self):
        hashes = match_core_eol.core_hashes(ROOT)
        from trading_lab import arena, exchange_lab, strategy_training
        self.assertEqual(hashes, {"training": strategy_training.source_hash(), "exchange": exchange_lab.source_hash(),
                                  "arena": arena.arena_source_hash()})


if __name__ == "__main__":
    unittest.main()
