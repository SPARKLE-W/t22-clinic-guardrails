param(
    [ValidateSet('start','stop')][string]$Action = 'start',
    [string]$BindAddress,
    [string]$PythonExe = $env:T22_APP_PYTHON,
    [string]$StateDirectory = 'C:\ProgramData\T22\formal-load'
)

$ErrorActionPreference = 'Stop'
$backendRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$servicePath = [System.IO.Path]::GetFullPath((Join-Path $backendRoot 'tests\performance\tbr05\02_formal_service.py'))
$candidatePath = Join-Path $backendRoot 'examples\08_严格负载部署候选.json'
$statePath = Join-Path $StateDirectory 'application-pool.json'

function Write-State($Value) {
    $temporaryPath = "$statePath.tmp"
    $Value | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $temporaryPath -Encoding UTF8
    Move-Item -LiteralPath $temporaryPath -Destination $statePath -Force
}

function Get-VerifiedProcess($Entry, $RecordedHost) {
    $process = Get-CimInstance Win32_Process -Filter "ProcessId=$($Entry.pid)" -ErrorAction SilentlyContinue
    if (-not $process) { return $null }
    $expectedPython = [System.IO.Path]::GetFullPath($Entry.python)
    $actualPython = [System.IO.Path]::GetFullPath($process.ExecutablePath)
    $command = [string]$process.CommandLine
    if ($actualPython -ne $expectedPython -or
        $command -notmatch [regex]::Escape($servicePath) -or
        $command -notmatch "--port\s+$($Entry.port)(\s|$)" -or
        $command -notmatch "--host\s+$([regex]::Escape($RecordedHost))(\s|$)") {
        throw "PID $($Entry.pid) no longer matches the recorded service identity; refusing to stop it."
    }
    $started = (Get-Process -Id $Entry.pid).StartTime.ToUniversalTime().Ticks
    if ([int64]$Entry.start_utc_ticks -ne $started) {
        throw "PID $($Entry.pid) start time changed; refusing to stop it."
    }
    return $process
}

function Get-PortListeners([int]$Port) {
    @(Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue)
}

if ($Action -eq 'stop') {
    if (-not (Test-Path -LiteralPath $statePath)) { Write-Output 'No formal-load application pool state.'; exit 0 }
    $state = Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json
    if ($state.status -eq 'stopped') { Write-Output 'Formal-load application pool is already stopped.'; exit 0 }
    if ($state.status -notin @('starting','running','failed')) { throw 'Unrecognized pool state; refusing to stop processes.' }

    $live = @()
    foreach ($entry in @($state.processes)) {
        $process = Get-VerifiedProcess $entry $state.bind_address
        if ($process) { $live += $process }
        elseif ((Get-PortListeners ([int]$entry.port)).Count -gt 0) {
            throw "Port $($entry.port) is listening without its recorded process; refusing to stop anything."
        }
    }
    foreach ($process in $live) { Stop-Process -Id $process.ProcessId -Force }
    $deadline = (Get-Date).AddSeconds(10)
    do {
        $remaining = @()
        foreach ($entry in @($state.processes)) {
            if ((Get-PortListeners ([int]$entry.port)).Count -gt 0) { $remaining += [int]$entry.port }
        }
        if ($remaining.Count -eq 0) { break }
        Start-Sleep -Milliseconds 250
    } while ((Get-Date) -lt $deadline)
    if ($remaining.Count -gt 0) { throw "Listeners remain on ports: $($remaining -join ', ')." }
    $state.status = 'stopped'
    $state.stopped_at_utc = [DateTime]::UtcNow.ToString('o')
    Write-State $state
    Write-Output 'Formal-load application pool stopped; state and logs retained.'
    exit 0
}

if (-not $BindAddress) { throw 'Pass -BindAddress with the current private IPv4 address of this application server.' }
try { $address = [System.Net.IPAddress]::Parse($BindAddress) } catch { throw 'BindAddress must be a private IPv4 address.' }
$octets = $address.GetAddressBytes()
$isPrivateV4 = $address.AddressFamily -eq [System.Net.Sockets.AddressFamily]::InterNetwork -and (
    $octets[0] -eq 10 -or
    ($octets[0] -eq 172 -and $octets[1] -ge 16 -and $octets[1] -le 31) -or
    ($octets[0] -eq 192 -and $octets[1] -eq 168)
)
if (-not $isPrivateV4) {
    throw 'BindAddress must be a private IPv4 address in the VPC.'
}
$localAddresses = @(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction Stop | ForEach-Object { $_.IPAddress })
if ($localAddresses -notcontains $BindAddress) {
    throw 'BindAddress is not assigned to this server; check the current private IPv4 address.'
}
if ($env:T22_FORMAL_LOAD -ne '1') { throw 'Set T22_FORMAL_LOAD=1 explicitly for the isolated formal-load service.' }
if (-not $env:T22_APP_CONFIG -or -not (Test-Path -LiteralPath $env:T22_APP_CONFIG)) {
    throw 'Set T22_APP_CONFIG to the private isolated-load JSON configuration.'
}
if (-not $PythonExe) { $PythonExe = (Get-Command python -ErrorAction Stop).Source }
$PythonExe = [System.IO.Path]::GetFullPath($PythonExe)
if (-not (Test-Path -LiteralPath $PythonExe)) { throw 'Python executable does not exist.' }

$configuration = Get-Content -LiteralPath $env:T22_APP_CONFIG -Raw | ConvertFrom-Json
$databaseName = [string]$configuration.database.NAME
if (-not $databaseName) { $databaseName = [string]$configuration.database.name }
if ($databaseName -notmatch '^t22_perf_[A-Za-z0-9_]+$') { throw 'Configuration must target a t22_perf_ isolated database.' }
$connectionAge = $configuration.database.CONN_MAX_AGE
if ($null -eq $connectionAge) { $connectionAge = $configuration.database.conn_max_age }
if ($null -ne $connectionAge -and [int]$connectionAge -ne 0) { throw 'This candidate requires CONN_MAX_AGE=0.' }
$healthChecks = $configuration.database.CONN_HEALTH_CHECKS
if ($null -eq $healthChecks) { $healthChecks = $configuration.database.conn_health_checks }
if ($null -ne $healthChecks -and [bool]$healthChecks) { throw 'This candidate requires CONN_HEALTH_CHECKS=false.' }
$allowedHosts = @($env:T22_ALLOWED_HOSTS -split ',' | ForEach-Object { $_.Trim() })
if ($allowedHosts -notcontains $BindAddress) { throw 'Include the current private BindAddress in T22_ALLOWED_HOSTS before starting.' }

$candidate = Get-Content -LiteralPath $candidatePath -Raw | ConvertFrom-Json
$processCount = [int]$candidate.application.processes
$threadCount = [int]$candidate.application.threads_per_process
$firstPort = [int]$candidate.application.first_port
$webBudget = [int]$candidate.connection_budget.web_maximum
$otherBudget = [int]$candidate.connection_budget.other_clients_maximum
$reserve = [int]$candidate.connection_budget.required_free_reserve
$totalBudget = [int]$candidate.connection_budget.total_budget
$maxConnections = [int]$candidate.mysql.max_connections
if ($processCount -lt 1 -or $threadCount -lt 1 -or $processCount * $threadCount -ne $webBudget -or
    $webBudget + $otherBudget + $reserve -gt $maxConnections -or
    $webBudget + $otherBudget -gt $totalBudget -or $totalBudget -gt $maxConnections) {
    throw 'Deployment candidate connection budget is inconsistent.'
}
if (-not (Test-Path -LiteralPath $servicePath)) { throw 'Formal-load service entry point is missing.' }
for ($index = 0; $index -lt $processCount; $index++) {
    $port = $firstPort + $index
    if ((Get-PortListeners $port).Count -gt 0) { throw "Port $port is already occupied; refusing to replace it." }
}

New-Item -ItemType Directory -Path $StateDirectory -Force | Out-Null
if (Test-Path -LiteralPath $statePath) {
    $previous = Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json
    if ($previous.status -ne 'stopped') { throw 'A previous pool state is not stopped; inspect it and run -Action stop first.' }
    $archivePath = Join-Path $StateDirectory "application-pool-stopped-$($previous.run_id).json"
    if (Test-Path -LiteralPath $archivePath) { throw 'Stopped state archive already exists; move it before starting a new run.' }
    Move-Item -LiteralPath $statePath -Destination $archivePath
}
$runId = Get-Date -Format 'yyyyMMdd_HHmmss_fff'
$logDirectory = Join-Path $StateDirectory "logs-$runId"
New-Item -ItemType Directory -Path $logDirectory | Out-Null
$state = [ordered]@{
    run_id = $runId; status = 'starting'; bind_address = $BindAddress
    database = $databaseName; python = $PythonExe; service = $servicePath
    log_directory = $logDirectory
    process_count = $processCount; threads_per_process = $threadCount
    started_at_utc = [DateTime]::UtcNow.ToString('o'); processes = @()
}
Write-State $state

try {
    for ($index = 0; $index -lt $processCount; $index++) {
        $port = $firstPort + $index
        $stdout = Join-Path $logDirectory "waitress-$port.stdout.log"
        $stderr = Join-Path $logDirectory "waitress-$port.stderr.log"
        $child = Start-Process -FilePath $PythonExe `
            -ArgumentList @('-B', "`"$servicePath`"", '--profile', '--host', $BindAddress, '--port', "$port", '--threads', "$threadCount") `
            -WorkingDirectory $backendRoot -WindowStyle Hidden -PassThru `
            -RedirectStandardOutput $stdout -RedirectStandardError $stderr
        $entry = [ordered]@{ pid = $child.Id; port = $port; python = $PythonExe; start_utc_ticks = (Get-Process -Id $child.Id).StartTime.ToUniversalTime().Ticks }
        $state.processes = @($state.processes) + $entry
        Write-State $state
    }

    foreach ($entry in @($state.processes)) {
        $deadline = (Get-Date).AddSeconds(20)
        do {
            $process = Get-VerifiedProcess $entry $BindAddress
            if (-not $process) { throw "Service process $($entry.pid) exited during startup." }
            $listener = Get-PortListeners ([int]$entry.port) | Where-Object { $_.LocalAddress -eq $BindAddress }
            if ($listener) { break }
            Start-Sleep -Milliseconds 250
        } while ((Get-Date) -lt $deadline)
        if (-not $listener) { throw "Service on port $($entry.port) did not become ready; inspect its log." }
    }
    $state.status = 'running'
    $state.ready_at_utc = [DateTime]::UtcNow.ToString('o')
    Write-State $state
    Write-Output "Formal-load pool ready: $processCount processes x $threadCount threads; ports $firstPort-$($firstPort + $processCount - 1); logs: $logDirectory"
}
catch {
    $failureMessage = $_.Exception.Message
    foreach ($entry in @($state.processes)) {
        try {
            $process = Get-VerifiedProcess $entry $BindAddress
            if ($process) { Stop-Process -Id $process.ProcessId -Force }
        } catch { Write-Warning $_.Exception.Message }
    }
    $state.status = 'failed'
    $state.failure = $failureMessage
    Write-State $state
    throw
}
