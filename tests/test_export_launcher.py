"""Owner export process recognition. All HTTP/process/export calls are fake."""
import json
from pathlib import Path
import shutil
import subprocess
import unittest

PS = shutil.which('powershell.exe')
ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'deployment/server/export_from_pc.ps1'


@unittest.skipUnless(PS, 'Windows PowerShell fixture required')
class ExportLauncherTests(unittest.TestCase):
    def invoke(self, command, *, active=False, process='python.exe', connected=True,
               incomplete=False, exchange_active=False, listening=True):
        values={'command':command,'active':active,'process':process,'connected':connected,
                'incomplete':incomplete,'exchange_active':exchange_active,'listening':listening}
        encoded=json.dumps(values).replace("'","''")
        harness=r'''
$fixture = 'VALUES' | ConvertFrom-Json
$global:fixtureStops=0; $global:fixtureExports=0
function Get-NetTCPConnection { if ($fixture.listening) { return [pscustomobject]@{OwningProcess=12345} } }
function Invoke-RestMethod {
    if (-not $fixture.connected) { throw 'fictional HTTP disconnect' }
    if ($fixture.incomplete) { return [pscustomobject]@{} }
    if ($args[0] -like '*/api/exchange/state') {
        $run=$null;if ($fixture.exchange_active) {$run=[pscustomobject]@{status='PAUSED'}}
        return [pscustomobject]@{active=[pscustomobject]@{arena_live=$run;workshop_demo=$null}}
    }
    return [pscustomobject]@{running=$fixture.active;action='new';arena_unfinished=$false;exchange_unfinished=$false}
}
function Get-CimInstance { return [pscustomobject]@{Name=$fixture.process;CommandLine=$fixture.command} }
function Stop-Process { $global:fixtureStops++ }
function Wait-Process { }
function Write-Host { }
function py { $global:fixtureExports++;$global:LASTEXITCODE=0 }
$failure=$null
try { & 'SCRIPT' | Out-Null } catch { $failure=$_.Exception.Message }
@{stops=$global:fixtureStops;exports=$global:fixtureExports;failure=$failure} | ConvertTo-Json -Compress
'''.replace('VALUES',encoded).replace('SCRIPT',str(SCRIPT).replace("'","''"))
        result=subprocess.run([PS,'-NoProfile','-ExecutionPolicy','Bypass','-Command',harness],capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)
        return json.loads(result.stdout)

    def test_current_cmd_and_scheduled_task_entrypoints_are_recognized(self):
        server=ROOT / 'deployment/server/run_server.py'
        for flags in ('-B -X utf8','-u',''):
            with self.subTest(flags=flags):
                result=self.invoke(f'"C:\\Fake Python\\python.exe" {flags} "{server}" serve --port 8765')
                self.assertIsNone(result['failure'],result)
                self.assertEqual((result['stops'],result['exports']),(1,1))

    def test_legacy_workspace_entrypoint_still_works(self):
        result=self.invoke(f'C:\\Fake\\pythonw.exe "{ROOT / "web_panel.py"}" --open',process='pythonw.exe')
        self.assertEqual((result['stops'],result['exports']),(1,1))

    def test_foreign_script_root_flags_and_port_never_stop_or_export(self):
        server=ROOT / 'deployment/server/run_server.py'
        for command in (f'C:\\Fake\\python.exe "C:\\Other\\deployment\\server\\run_server.py" serve --port 8765',
                        f'C:\\Fake\\python.exe "{server}" serve --port 8766',
                        f'C:\\Fake\\python.exe -c "{server}" serve --port 8765',
                        f'C:\\Fake\\python.exe "{server}" check',
                        f'C:\\Fake\\python.exe "{server}" serve --port 8765 --extra'):
            with self.subTest(command=command):
                result=self.invoke(command)
                self.assertEqual((result['stops'],result['exports']),(0,0))
                self.assertIsNotNone(result['failure'])

    def test_active_or_disconnected_panel_cannot_be_stopped(self):
        command=f'C:\\Fake\\python.exe "{ROOT / "deployment/server/run_server.py"}" serve --port 8765'
        for options in ({'active':True},{'connected':False},{'process':'foreign.exe'},
                        {'incomplete':True},{'exchange_active':True}):
            with self.subTest(options=options):
                result=self.invoke(command,**options)
                self.assertEqual((result['stops'],result['exports']),(0,0))

    def test_owner_closed_panel_can_export_without_stopping_any_process(self):
        result=self.invoke('',listening=False)
        self.assertIsNone(result['failure'])
        self.assertEqual((result['stops'],result['exports']),(0,1))


if __name__=='__main__':unittest.main()
