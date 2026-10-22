param([ValidateSet('start','stop')][string]$Action='start')
$ErrorActionPreference='Stop'
$appRuntime=Join-Path $env:USERPROFILE '.cache/t22-runtime'
$appState=Join-Path $appRuntime 'app-http.json'
$appPort=18730
$appPython=if($env:T22_APP_PYTHON){$env:T22_APP_PYTHON}else{Join-Path $appRuntime 'controls-venv/Scripts/python.exe'}
$appPython=[System.IO.Path]::GetFullPath($appPython)
$appCode=Split-Path $PSScriptRoot -Parent
if($Action -eq 'stop') {
    if(-not(Test-Path -LiteralPath $appState)){ Write-Output 'No local app state.'; exit 0 }
    $saved=Get-Content -LiteralPath $appState -Raw | ConvertFrom-Json
    $saved.exe=[System.IO.Path]::GetFullPath($saved.exe)
    $launcher=Get-Process -Id $saved.pid -ErrorAction SilentlyContinue
    if($launcher){
        if($launcher.Path -ne $saved.exe -or $launcher.StartTime.ToUniversalTime().Ticks -ne ([datetime]$saved.started).ToUniversalTime().Ticks){throw 'Launcher identity changed; refuse stop.'}
        $listener=Get-NetTCPConnection -State Listen -LocalAddress '127.0.0.1' -LocalPort $appPort -ErrorAction SilentlyContinue
        if($listener){
            $server=Get-CimInstance Win32_Process -Filter "ProcessId=$($listener.OwningProcess)"
            if($server.ProcessId -ne $launcher.Id -and ($server.ParentProcessId -ne $launcher.Id -or $server.CommandLine -notmatch 'manage.py.*runserver.*127.0.0.1:18730')){throw 'Listener does not belong to this launcher; refuse stop.'}
            if($saved.server_pid -and $listener.OwningProcess -ne $saved.server_pid){throw 'Listener PID changed; refuse stop.'}
            Stop-Process -Id $listener.OwningProcess -Force
        }
        $launcher=Get-Process -Id $saved.pid -ErrorAction SilentlyContinue
        if($launcher -and $launcher.Path -eq $saved.exe -and $launcher.StartTime.ToUniversalTime().Ticks -eq ([datetime]$saved.started).ToUniversalTime().Ticks){Stop-Process -InputObject $launcher -Force -ErrorAction SilentlyContinue}
    }
    for($attempt=0;$attempt -lt 10;$attempt++){
        $remaining=Get-NetTCPConnection -State Listen -LocalPort $appPort -ErrorAction SilentlyContinue
        if(-not $remaining){break}
        Start-Sleep -Milliseconds 250
    }
    if($remaining){throw 'App port still listening; no unrelated process targeted.'}
    Write-Output 'Local app stopped; database is managed separately.'
    exit 0
}
if(Get-NetTCPConnection -State Listen -LocalPort $appPort -ErrorAction SilentlyContinue){throw 'App port occupied; refuse replace.'}
if(-not $env:T22_APP_CONFIG){$env:T22_APP_CONFIG=Join-Path $appRuntime 'private/app.json'}
if(-not(Test-Path -LiteralPath $env:T22_APP_CONFIG)){throw 'Set T22_APP_CONFIG to a private configuration first.'}
$appLogs=Join-Path $appRuntime 'logs'
New-Item -ItemType Directory -Path $appLogs -Force | Out-Null
$stamp=Get-Date -Format 'yyyyMMdd_HHmmss_fff'
$launcher=Start-Process -FilePath $appPython -ArgumentList @('-B','manage.py','runserver','127.0.0.1:18730','--noreload') -WorkingDirectory $appCode -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $appLogs "app-$stamp.stdout.txt") -RedirectStandardError (Join-Path $appLogs "app-$stamp.stderr.txt")
$saved=@{pid=$launcher.Id;started=$launcher.StartTime.ToUniversalTime().ToString('o');exe=$appPython;port=$appPort}
$saved | ConvertTo-Json | Set-Content -LiteralPath $appState -Encoding utf8
for($attempt=0;$attempt -lt 30;$attempt++){
    Start-Sleep -Milliseconds 250
    $listener=Get-NetTCPConnection -State Listen -LocalAddress '127.0.0.1' -LocalPort $appPort -ErrorAction SilentlyContinue
    if($listener){
        $server=Get-Process -Id $listener.OwningProcess
        $saved.server_pid=$server.Id
        $saved.server_started=$server.StartTime.ToUniversalTime().ToString('o')
        $saved | ConvertTo-Json | Set-Content -LiteralPath $appState -Encoding utf8
        Write-Output 'Local app ready at http://127.0.0.1:18730/'; exit 0
    }
    if($launcher.HasExited){throw 'Local app exited; inspect private runtime logs.'}
}
throw 'Local app readiness timed out; state retained for explicit stop.'
