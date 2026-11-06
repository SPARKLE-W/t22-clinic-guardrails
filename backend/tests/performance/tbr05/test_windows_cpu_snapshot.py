import json
import shutil
import subprocess
import unittest
from pathlib import Path


@unittest.skipUnless(shutil.which('powershell.exe'), 'Windows PowerShell required')
class WindowsCpuSnapshotTests(unittest.TestCase):
    def test_process_identity_cpu_split_and_unreadable_values(self):
        script = Path(__file__).with_name('04_sample_windows_resources.ps1')
        command = r'''
$ErrorActionPreference = 'Stop'
$tokens = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile('__SCRIPT__', [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw 'PowerShell parse errors' }
$function = $ast.Find({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Get-ProcessCpuSnapshot' }, $true)
if (-not $function) { throw 'Missing process CPU snapshot function' }
Invoke-Expression $function.Extent.Text
$good = [pscustomobject]@{ Id=123; ProcessName='other-service'; StartTime=[datetime]'2026-01-01Z'; TotalProcessorTime=[timespan]::FromSeconds(7); UserProcessorTime=[timespan]::FromSeconds(5); PrivilegedProcessorTime=[timespan]::FromSeconds(2) }
$bad = [pscustomobject]@{ Id=456; ProcessName='protected' }
$bad | Add-Member ScriptProperty TotalProcessorTime { throw 'Access denied' }
$idle = [pscustomobject]@{ Id=0; ProcessName='Idle' }
@(Get-ProcessCpuSnapshot @($good, $bad, $idle)) | ConvertTo-Json -Depth 5 -Compress
'''.replace('__SCRIPT__', str(script).replace("'", "''"))
        result = subprocess.run(['powershell.exe', '-NoProfile', '-Command', command], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = json.loads(result.stdout)
        self.assertEqual([row['pid'] for row in rows], [123, 456])
        self.assertEqual(rows[0]['cpu_cumulative_seconds'], 7)
        self.assertEqual(rows[0]['user_cpu_cumulative_seconds'], 5)
        self.assertEqual(rows[0]['privileged_cpu_cumulative_seconds'], 2)
        self.assertTrue(rows[0]['started_utc'].startswith('2026-01-01'))
        self.assertIsNone(rows[0]['sampling_error'])
        self.assertIsNone(rows[1]['cpu_cumulative_seconds'])
        self.assertIsNone(rows[1]['started_utc'])
        self.assertTrue(rows[1]['sampling_error'])
