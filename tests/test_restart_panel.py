"""Owner updater safeguards with all HTTP/process operations replaced by fakes."""
import json
from pathlib import Path
import shutil
import subprocess
import unittest


POWERSHELL = shutil.which("powershell.exe")
SCRIPT = Path(__file__).resolve().parents[1] / "tools/restart_panel.ps1"


@unittest.skipUnless(POWERSHELL, "Windows PowerShell fixture required")
class RestartPanelTests(unittest.TestCase):
    def invoke_fake(self, state, *, disconnected=False, expected_process=True):
        # These functions shadow every HTTP/process entrypoint in the script;
        # no real listener, server, launcher or process is inspected/modified.
        encoded = json.dumps(state, separators=(",", ":"))
        script = str(SCRIPT).replace("'", "''")
        server = str(SCRIPT.parent.parent / "web_panel.py").replace("'", "''")
        harness = r'''
$global:fixtureStops = 0; $global:fixtureStarts = 0
function Test-Path { return $true }
function Get-NetTCPConnection { return [pscustomobject]@{OwningProcess=12345} }
function Invoke-RestMethod {
    if (DISCONNECTED) { throw 'fictional HTTP disconnect' }
    return ('STATE' | ConvertFrom-Json)
}
function Get-CimInstance {
    return [pscustomobject]@{Name='PROCESS'; CommandLine='C:\Fixtures\pythonw.exe "SERVER" --open'}
}
function Stop-Process { $global:fixtureStops += 1 }
function Wait-Process { }
function Start-Process { $global:fixtureStarts += 1 }
$failure = $null
try { & 'SCRIPT' | Out-Null } catch { $failure = $_.Exception.Message }
@{stops=$global:fixtureStops;starts=$global:fixtureStarts;failure=$failure} | ConvertTo-Json -Compress
'''.replace("DISCONNECTED", "$true" if disconnected else "$false").replace("STATE", encoded).replace(
            "PROCESS", "pythonw.exe" if expected_process else "foreign.exe").replace("SERVER", server).replace("SCRIPT", script)
        result = subprocess.run([POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", harness],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    @staticmethod
    def idle():
        return {"running": False, "action": "new", "arena_unfinished": False, "exchange_unfinished": False}

    def test_disconnected_or_incomplete_status_never_stops_or_launches(self):
        for state, disconnected in ((self.idle(), True), ({"action": "new"}, False)):
            with self.subTest(state=state, disconnected=disconnected):
                result = self.invoke_fake(state, disconnected=disconnected)
                self.assertEqual((result["stops"], result["starts"]), (0, 0))
                self.assertIn("No process was stopped", result["failure"])

    def test_unfinished_work_and_foreign_process_preserve_restart_guard(self):
        for key in ("running", "arena_unfinished", "exchange_unfinished"):
            with self.subTest(key=key):
                state = self.idle()
                state[key] = True
                result = self.invoke_fake(state)
                self.assertEqual((result["stops"], result["starts"]), (0, 0))
                self.assertIsNotNone(result["failure"])
        foreign = self.invoke_fake(self.idle(), expected_process=False)
        self.assertEqual((foreign["stops"], foreign["starts"]), (0, 0))

    def test_verified_idle_server_can_update_only_in_the_process_fixture(self):
        result = self.invoke_fake(self.idle())
        self.assertIsNone(result["failure"])
        self.assertEqual((result["stops"], result["starts"]), (1, 1))


if __name__ == "__main__":
    unittest.main()
