$ErrorActionPreference = 'Stop'
$stagePath = Split-Path $PSScriptRoot -Parent
$osInfo = Get-CimInstance Win32_OperatingSystem | Select-Object Caption, Version, OSArchitecture, TotalVisibleMemorySize, FreePhysicalMemory
$cpuInfo = @(Get-CimInstance Win32_Processor | Select-Object Name, NumberOfCores, NumberOfLogicalProcessors)
$diskInfo = @(Get-CimInstance Win32_LogicalDisk -Filter "DriveType=3" | Where-Object { $_.DeviceID -in @('C:', 'D:') } | Select-Object DeviceID, Size, FreeSpace)
$commandInfo = @()
foreach ($toolName in @('python','py','node','npm','mysql','psql','docker','erl','rabbitmq-server','rabbitmqctl')) {
    $foundTools = @(Get-Command $toolName -CommandType Application -ErrorAction SilentlyContinue)
    $commandInfo += [pscustomobject]@{ name=$toolName; found=($foundTools.Count -gt 0); paths=@($foundTools | ForEach-Object Source) }
}
$serviceInfo = @(Get-Service | Where-Object { $_.Name -match 'mysql|postgres|rabbitmq|erlang|docker' -or $_.DisplayName -match 'mysql|postgres|rabbitmq|erlang|docker' } | Select-Object Name, DisplayName, @{Name='Status';Expression={$_.Status.ToString()}})
$appInfo = @()
foreach ($registryPath in @('HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*','HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*','HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*')) {
    $appInfo += @(Get-ItemProperty -Path $registryPath -ErrorAction SilentlyContinue | Where-Object { $_.DisplayName -match 'MySQL|PostgreSQL|Erlang|RabbitMQ|Docker|Node.js|Python 3' } | Select-Object DisplayName, DisplayVersion)
}
$knownPorts = @(3306,5432,5672,15672,8000,8080,5173)
$listeners = @(Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | Where-Object { $_.LocalPort -in $knownPorts } | Select-Object LocalAddress, LocalPort)
$result = [ordered]@{
    collected_at=(Get-Date).ToString('o'); action='read_only_inventory';
    os=$osInfo; cpu=$cpuInfo; disks=$diskInfo; commands=$commandInfo;
    matching_services=$serviceInfo; matching_registered_apps=$appInfo;
    candidate_port_listeners=$listeners;
    limits=@('PATH/registry/service/listener checks cannot prove a component is absent or ready.','No database login, Docker daemon query, package installation, service modification or workload was run.')
}
$result | ConvertTo-Json -Depth 7 | Set-Content -LiteralPath (Join-Path $stagePath '10_本机条件原始盘点.json') -Encoding utf8
$result | ConvertTo-Json -Depth 7
