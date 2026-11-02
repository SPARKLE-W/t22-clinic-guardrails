param([ValidateSet('start','stop')][string]$Action='start', [ValidateSet('serve.py','serve_profile.py')][string]$Entry='serve.py', [ValidateRange(18722,18729)][int]$Port=18722, [ValidateRange(1,64)][int]$Threads=32)
$ErrorActionPreference='Stop'
$stageDir=Split-Path $PSScriptRoot -Parent
$codeDir=Join-Path $stageDir '实验代码'
$runtimeDir=Join-Path $env:USERPROFILE '.cache/t22-runtime'
$pythonExe=Join-Path $runtimeDir 'venv/Scripts/python.exe'
$stateFile=Join-Path $runtimeDir $(if($Entry -eq 'serve.py'){'probe-http.json'}else{"probe-profile-http-$Port.json"})
$logDir=Join-Path $stageDir $(if($Entry -eq 'serve.py'){'原始结果/HTTP'}else{"原始结果/性能定位/HTTP_$Port"})
New-Item -ItemType Directory -Path $logDir -Force | Out-Null
if ($Action -eq 'start') {
    if (Get-NetTCPConnection -LocalAddress '127.0.0.1' -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) { throw 'Task port already listening; refuse to replace.' }
    $logStamp=Get-Date -Format 'yyyyMMdd_HHmmss'
    foreach ($logName in @('server.stdout.txt','server.stderr.txt')) {
        $previousLog=Join-Path $logDir $logName
        if (Test-Path -LiteralPath $previousLog) { Copy-Item -LiteralPath $previousLog -Destination (Join-Path $logDir ($logStamp+'_'+$logName)) }
    }
    $entryArguments=if($Entry -eq 'serve.py'){"-u $Entry"}else{"-u $Entry --port $Port --threads $Threads"}
    $process=Start-Process -FilePath $pythonExe -ArgumentList $entryArguments -WorkingDirectory $codeDir -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $logDir 'server.stdout.txt') -RedirectStandardError (Join-Path $logDir 'server.stderr.txt')
    @{pid=$process.Id;started=$process.StartTime.ToUniversalTime().ToString('o');exe=$pythonExe} | ConvertTo-Json | Set-Content -LiteralPath $stateFile -Encoding utf8
    for($attempt=0;$attempt -lt 30;$attempt++) {
        Start-Sleep -Milliseconds 250
        $listener=Get-NetTCPConnection -LocalAddress '127.0.0.1' -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
        if ($listener) {
            $serverProcess=Get-Process -Id $listener.OwningProcess
            @{pid=$process.Id;started=$process.StartTime.ToUniversalTime().ToString('o');exe=$pythonExe;server_pid=$serverProcess.Id;server_started=$serverProcess.StartTime.ToUniversalTime().ToString('o');server_exe=$serverProcess.Path} | ConvertTo-Json | Set-Content -LiteralPath $stateFile -Encoding utf8
            Write-Output "Task HTTP ready at 127.0.0.1:$Port ($Threads threads)."; exit 0
        }
        if ($process.HasExited) { throw 'HTTP exited before readiness; inspect task stderr log.' }
    }
    throw 'HTTP readiness timed out.'
} else {
    if (-not(Test-Path -LiteralPath $stateFile)) { Write-Output 'No task HTTP state.'; exit 0 }
    $state=Get-Content -LiteralPath $stateFile -Raw | ConvertFrom-Json
    $current=Get-Process -Id $state.pid -ErrorAction SilentlyContinue
    if ($current) {
        if ($current.Path -ne $state.exe -or $current.StartTime.ToUniversalTime().Ticks -ne ([datetime]$state.started).ToUniversalTime().Ticks) { throw 'PID identity changed; refuse stop.' }
        $listener=Get-NetTCPConnection -LocalAddress '127.0.0.1' -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
        if ($listener) {
            $serverCim=Get-CimInstance Win32_Process -Filter "ProcessId=$($listener.OwningProcess)"
            if ($serverCim.ProcessId -ne $current.Id -and ($serverCim.ParentProcessId -ne $current.Id -or $serverCim.CommandLine -notmatch ([regex]::Escape($Entry)))) { throw 'Listener is not this task launcher child; refuse stop.' }
            if ($state.server_pid -and $listener.OwningProcess -ne $state.server_pid) { throw 'Listener PID changed; refuse stop.' }
            Stop-Process -Id $listener.OwningProcess -Force
        }
        $current=Get-Process -Id $state.pid -ErrorAction SilentlyContinue
        if ($current) { Stop-Process -Id $current.Id -Force -ErrorAction SilentlyContinue }
    }
    for ($attempt=0;$attempt -lt 10;$attempt++) {
        $remaining=Get-NetTCPConnection -LocalAddress '127.0.0.1' -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
        if (-not $remaining) { break }
        Start-Sleep -Milliseconds 250
    }
    if ($remaining) { throw 'Task listener still present after bounded stop wait.' }
    Write-Output 'Task HTTP stopped; no other process targeted.'
    exit 0
}
