param([ValidateSet('validate','start','stop','status')][string]$Action='status')
$ErrorActionPreference='Stop'
$stageDir=Split-Path $PSScriptRoot -Parent
$runtimeDir=Join-Path $env:USERPROFILE '.cache/t22-runtime'
$proxyExe=[IO.Path]::GetFullPath((Join-Path $runtimeDir 'bin/caddy-2.11.7/caddy.exe'))
$configFile=Join-Path $runtimeDir 'config/caddy/Caddyfile'
$stateFile=Join-Path $runtimeDir 'caddy-proxy-state.json'
$evidenceDir=Join-Path $stageDir '原始结果/性能定位/统一入口'
New-Item -ItemType Directory -Path $evidenceDir -Force | Out-Null
$stamp=Get-Date -Format 'yyyyMMdd_HHmmss_fff'
function Get-ProxyIdentity {
    if(-not(Test-Path -LiteralPath $stateFile)){ return $null }
    $state=Get-Content -LiteralPath $stateFile -Raw | ConvertFrom-Json
    $current=Get-Process -Id $state.pid -ErrorAction SilentlyContinue
    if(-not $current){ return $null }
    if($current.Path -ne $state.exe -or $current.StartTime.ToUniversalTime().Ticks -ne ([datetime]$state.started_utc).ToUniversalTime().Ticks){
        throw 'Recorded proxy PID identity changed; refuse to target it.'
    }
    return $current
}
function Get-ProxyListeners {
    return @(Get-NetTCPConnection -State Listen -LocalPort 18726 -ErrorAction SilentlyContinue)
}
function Write-ProxyEvent($details) {
    $details.utc=[datetime]::UtcNow.ToString('o')
    $details.action=$Action
    $details | ConvertTo-Json -Depth 7 | Set-Content -LiteralPath (Join-Path $evidenceDir ($stamp+'_'+$Action+'.json')) -Encoding utf8
}
if($Action -eq 'status') {
    $current=Get-ProxyIdentity
    $listeners=@(Get-ProxyListeners | Select-Object LocalAddress,LocalPort,OwningProcess)
    $status=@{running=($null -ne $current);task_pid=$(if($current){$current.Id}else{$null});listeners=$listeners}
    Write-ProxyEvent $status
    $status | ConvertTo-Json -Depth 4
    exit 0
}
if($Action -eq 'stop') {
    $current=Get-ProxyIdentity
    $listeners=Get-ProxyListeners
    if($listeners -and (-not $current -or @($listeners | Where-Object OwningProcess -ne $current.Id).Count)) {
        throw '18726 belongs to a different process; refuse stop.'
    }
    $stoppedPid=$null
    if($current) {
        $stoppedPid=$current.Id
        # Admin API is disabled. Call only after all probe requests have drained.
        Stop-Process -Id $current.Id -Force
        $current.WaitForExit(10000) | Out-Null
        if(-not $current.HasExited){ throw 'Task proxy process exit unconfirmed.' }
    }
    for($attempt=0;$attempt -lt 10;$attempt++) {
        $remaining=Get-ProxyListeners
        if(-not $remaining){ break }
        Start-Sleep -Milliseconds 250
    }
    if($remaining){ throw '18726 still listening after task proxy stop.' }
    Write-ProxyEvent @{stopped_pid=$stoppedPid;running=$false;listeners=@();stop_method='verified task PID termination; admin API disabled'}
    Write-Output 'Task proxy stopped; 18726 has no listener.'
    exit 0
}
if(-not(Test-Path -LiteralPath $proxyExe)){ throw 'Verified task Caddy binary missing.' }
if(-not(Test-Path -LiteralPath $configFile)){ throw 'Task Caddyfile missing.' }
$previousData=$env:XDG_DATA_HOME
$previousConfig=$env:XDG_CONFIG_HOME
$startedThisCall=$false
try {
    $env:XDG_DATA_HOME=Join-Path $runtimeDir 'data/caddy-xdg'
    $env:XDG_CONFIG_HOME=Join-Path $runtimeDir 'config/caddy-xdg'
    if($Action -eq 'validate') {
        $stdout=Join-Path $evidenceDir ($stamp+'_validate.stdout.txt')
        $stderr=Join-Path $evidenceDir ($stamp+'_validate.stderr.txt')
        $check=Start-Process -FilePath $proxyExe -ArgumentList @('validate','--config',$configFile,'--adapter','caddyfile') -WorkingDirectory $runtimeDir -WindowStyle Hidden -PassThru -Wait -RedirectStandardOutput $stdout -RedirectStandardError $stderr
        Write-ProxyEvent @{exit_code=$check.ExitCode;config_sha256=(Get-FileHash -LiteralPath $configFile -Algorithm SHA256).Hash;service_started=$false;stdout=$stdout;stderr=$stderr}
        if($check.ExitCode -ne 0){ throw 'Caddy validation failed; see task evidence.' }
        Write-Output 'Caddy configuration validated; no listener started.'
        exit 0
    }
    $current=Get-ProxyIdentity
    if($current){ throw 'Task proxy already running; refuse duplicate start.' }
    if(Get-ProxyListeners){ throw '18726 occupied; refuse to replace any listener.' }
    $stdout=Join-Path $evidenceDir ($stamp+'_server.stdout.txt')
    $stderr=Join-Path $evidenceDir ($stamp+'_server.stderr.txt')
    $process=Start-Process -FilePath $proxyExe -ArgumentList @('run','--config',$configFile,'--adapter','caddyfile') -WorkingDirectory $runtimeDir -WindowStyle Hidden -PassThru -RedirectStandardOutput $stdout -RedirectStandardError $stderr
    $startedThisCall=$true
    $state=@{pid=$process.Id;started_utc=$process.StartTime.ToUniversalTime().ToString('o');exe=$proxyExe;port=18726;config=$configFile;config_sha256=(Get-FileHash -LiteralPath $configFile -Algorithm SHA256).Hash;stdout=$stdout;stderr=$stderr}
    $state | ConvertTo-Json | Set-Content -LiteralPath $stateFile -Encoding utf8
    for($attempt=0;$attempt -lt 25;$attempt++) {
        Start-Sleep -Milliseconds 200
        if($process.HasExited){ throw 'Task proxy exited before readiness; see task evidence.' }
        $listeners=Get-ProxyListeners
        if($listeners) {
            if(@($listeners | Where-Object { $_.OwningProcess -ne $process.Id -or $_.LocalAddress -ne '127.0.0.1' }).Count){ throw 'Proxy listener ownership or binding mismatch.' }
            Write-ProxyEvent @{ready=$true;state=$state;listeners=@($listeners | Select-Object LocalAddress,LocalPort,OwningProcess)}
            Write-Output 'Task proxy ready at http://127.0.0.1:18726; four loopback upstreams.'
            exit 0
        }
    }
    throw 'Task proxy readiness timed out.'
} catch {
    if($startedThisCall) {
        $owned=Get-ProxyIdentity
        if($owned) { Stop-Process -Id $owned.Id -Force; $owned.WaitForExit(10000) | Out-Null }
        Write-ProxyEvent @{ready=$false;error=$_.Exception.Message;cleanup='terminated verified proxy started by this call'}
    }
    throw
} finally {
    $env:XDG_DATA_HOME=$previousData
    $env:XDG_CONFIG_HOME=$previousConfig
}
