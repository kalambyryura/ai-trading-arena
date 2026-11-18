"""Actual cross-process research lease; tiny temp files, no exchange or history."""
from contextlib import redirect_stderr
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from research_limits import exclusive_research_slot, shared_repository_root
import arena_eval
import author_backtest

PROBE = """
from tools.research_limits import ResearchBusy, exclusive_research_slot
import sys
try:
    with exclusive_research_slot(sys.argv[1]):
        print('acquired', flush=True)
        if len(sys.argv) > 2:
            sys.stdin.read(1)
except ResearchBusy:
    sys.exit(17)
"""


def probe(path):
    return subprocess.run([sys.executable, "-B", "-c", PROBE, str(path)], cwd=ROOT,
                          capture_output=True, text=True, timeout=8)


class ResearchLimitTests(unittest.TestCase):
    def test_second_process_is_rejected_without_wait_or_retry_and_release_allows_next(self):
        with tempfile.TemporaryDirectory() as folder:
            with exclusive_research_slot(folder):
                self.assertEqual(probe(folder).returncode, 17)
            self.assertEqual(probe(folder).returncode, 0)

    def test_worktrees_share_the_primary_repository_lease(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            primary = root / "primary"
            common = primary / ".git"
            branches = []
            for name in ("a", "b"):
                gitdir = common / "worktrees" / name
                gitdir.mkdir(parents=True)
                (gitdir / "commondir").write_text("../..\n", encoding="utf-8")
                tree = root / name
                tree.mkdir()
                (tree / ".git").write_text("gitdir: " + str(gitdir) + "\n", encoding="utf-8")
                branches.append(tree)
                self.assertEqual(shared_repository_root(tree), primary.resolve())
            with exclusive_research_slot(branches[0]):
                self.assertEqual(probe(branches[1]).returncode, 17)
            self.assertEqual(probe(branches[1]).returncode, 0)

    def test_terminating_test_owner_releases_os_lease_without_deleting_or_resetting_file(self):
        with tempfile.TemporaryDirectory() as folder:
            owner = subprocess.Popen([sys.executable, "-B", "-c", PROBE, folder, "hold"], cwd=ROOT,
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                self.assertEqual(owner.stdout.readline().strip(), "acquired")
                self.assertEqual(probe(folder).returncode, 17)
                owner.terminate()
                owner.communicate(timeout=8)
                self.assertEqual(probe(folder).returncode, 0)
                self.assertEqual((Path(folder) / "runtime/research_resources/heavy.lock").read_bytes(), b"0")
            finally:
                if owner.poll() is None:
                    owner.kill()
                    owner.communicate(timeout=8)

    def test_parallel_jobs_are_rejected_before_any_data_or_evaluation(self):
        with patch.object(arena_eval, "one_window") as run, redirect_stderr(io.StringIO()):
            for jobs in ("0", "2", "12", "-1"):
                with self.assertRaises(SystemExit) as result:
                    arena_eval.main(["--recipe", "missing.json", "--jobs", jobs])
                self.assertEqual(result.exception.code, 2)
            run.assert_not_called()

    def test_backtest_cli_is_blocked_before_numpy_and_data_loading(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(author_backtest, "ROOT", Path(folder)), patch.object(author_backtest, "need_numpy") as load:
                with exclusive_research_slot(folder), self.assertRaises(SystemExit):
                    author_backtest.main(["check-features", "--data", "missing.csv"])
                load.assert_not_called()


if __name__ == "__main__":
    unittest.main()
