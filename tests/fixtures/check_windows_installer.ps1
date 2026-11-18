param([string]$Installer)
$ErrorActionPreference='Stop'
# Parse the installer. Execute ONLY its pure OS gate against invented CIM values.
# No install, ACL, process, network or ScheduledTask command is executed.
$tokens=$null; $parseErrors=$null
$ast=[System.Management.Automation.Language.Parser]::ParseFile($Installer,[ref]$tokens,[ref]$parseErrors)
if ($parseErrors.Count) { throw 'installer_parse_failed' }
$gates=@($ast.FindAll({param($node) $node -is [System.Management.Automation.Language.IfStatementAst] -and $node.Extent.Text.Contains('ProductType')},$true))
if ($gates.Count -ne 1) { throw 'installer_os_gate_ambiguous' }
$guard=[ScriptBlock]::Create($gates[0].Extent.Text)
$result=@()
foreach ($sample in @(@(1,'10.0.19045',$true),@(1,'10.0.26100',$true),@(2,'10.0.20348',$true),@(3,'10.0.20348',$true),@(1,'6.1.7601',$false),@(0,'10.0',$false))) {
    $systemInfo=[pscustomobject]@{ProductType=$sample[0];Version=$sample[1]}
    $accepted=$true
    try { & $guard } catch { $accepted=$false }
    if ($accepted -ne $sample[2]) { throw 'installer_os_matrix_failed' }
    $result+=@{product_type=$sample[0];version=$sample[1];accepted=$accepted}
}
@{parse='PASS';matrix=$result;installer_executed=$false;tasks_registered=0}|ConvertTo-Json -Depth 4 -Compress
