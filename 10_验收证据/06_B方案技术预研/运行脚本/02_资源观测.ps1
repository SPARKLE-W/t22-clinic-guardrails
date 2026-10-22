param([int]$DurationSeconds=20,[int]$IntervalMilliseconds=1000)
$ErrorActionPreference='Stop'
if ($DurationSeconds -lt 1 -or $DurationSeconds -gt 60 -or $IntervalMilliseconds -lt 250) { throw 'Invalid bounded sampling interval.' }
$stageDir=Split-Path $PSScriptRoot -Parent
$outputFile=Join-Path $stageDir '原始结果/resource_diag_resources.json'
$venvLauncher=[System.IO.Path]::GetFullPath((Join-Path $env:USERPROFILE '.cache/t22-runtime/venv/Scripts/python.exe'))
$ports=@{http=18722;mysql=13322;rabbitmq=15672}
$serverIds=@{}
# Resolve actual listening servers; do not sample their launcher PID instead.
$listeners=@(Get-NetTCPConnection -State Listen | Where-Object { $_.LocalAddress -eq '127.0.0.1' -and $_.LocalPort -in $ports.Values })
foreach($role in $ports.Keys) {
    $found=@($listeners | Where-Object LocalPort -eq $ports[$role] | Select-Object -ExpandProperty OwningProcess -Unique)
    if($found.Count -ne 1) { throw ('Expected one loopback listener for '+$role) }
    $proc=Get-Process -Id $found[0]
    $serverIds[$role]=@{pid=$proc.Id;started_utc=$proc.StartTime.ToUniversalTime().ToString('o');executable=$proc.Path;port=$ports[$role]}
}
function Read-ProcessSample($role,$processId,$expectedStarted) {
    $proc=Get-Process -Id $processId -ErrorAction SilentlyContinue
    if(-not $proc) { return @{role=$role;pid=$processId;present=$false} }
    $started=$proc.StartTime.ToUniversalTime().ToString('o')
    if($expectedStarted -and $started -ne $expectedStarted) { return @{role=$role;pid=$processId;present=$false;identity_changed=$true} }
    return @{role=$role;pid=$proc.Id;present=$true;started_utc=$started;executable=$proc.Path;
        cpu_cumulative_seconds=[double]$proc.CPU;rss_bytes=[long]$proc.WorkingSet64;thread_count=$proc.Threads.Count}
}
$samples=[System.Collections.Generic.List[object]]::new()
$startedUtc=[datetime]::UtcNow.ToString('o')
$timer=[System.Diagnostics.Stopwatch]::StartNew()
$nextMilliseconds=0
Write-Output 'Resource sampling started; output contains no process command lines.'
while($timer.Elapsed.TotalSeconds -lt $DurationSeconds) {
    $sampleUtc=[datetime]::UtcNow.ToString('o')
    $processSamples=[System.Collections.Generic.List[object]]::new()
    foreach($role in @('http','mysql','rabbitmq')) {
        $identity=$serverIds[$role]
        $processSamples.Add((Read-ProcessSample $role $identity.pid $identity.started_utc))
    }
    # CommandLine is used only for local matching, never copied to output.
    $candidates=@(Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object {
        $_.CommandLine -match 'load_probe\.py\b' -and $_.CommandLine -match '--tag(?:\s+|=)["'']?resource_diag(?:["'']?)(?:\s|$)'
    })
    $parentIds=@($candidates | Select-Object -ExpandProperty ParentProcessId)
    $actualLoad=@($candidates | Where-Object { $_.ProcessId -notin $parentIds -and $_.ExecutablePath -ne $venvLauncher })
    foreach($candidate in $actualLoad) {
        $loadSample=Read-ProcessSample 'load_generator' $candidate.ProcessId $null
        $loadSample.parent_pid=$candidate.ParentProcessId
        $loadSample.parent_is_matching_launcher=($candidate.ParentProcessId -in @($candidates.ProcessId))
        $processSamples.Add($loadSample)
    }
    $os=Get-CimInstance Win32_OperatingSystem
    $samples.Add(@{utc=$sampleUtc;elapsed_seconds=$timer.Elapsed.TotalSeconds;processes=$processSamples.ToArray();
        load_process_matches=$actualLoad.Count;os_available_memory_bytes=([long]$os.FreePhysicalMemory*1024)})
    $nextMilliseconds+=$IntervalMilliseconds
    $remaining=$nextMilliseconds-$timer.Elapsed.TotalMilliseconds
    if($remaining -gt 0) { Start-Sleep -Milliseconds ([int][math]::Ceiling($remaining)) }
}
$result=@{kind='resource_observation_only_not_bottleneck_proof';requested_seconds=$DurationSeconds;interval_milliseconds=$IntervalMilliseconds;
    started_utc=$startedUtc;ended_utc=[datetime]::UtcNow.ToString('o');elapsed_seconds=$timer.Elapsed.TotalSeconds;
    logical_processors=[Environment]::ProcessorCount;resolved_listening_servers=$serverIds;sample_count=$samples.Count;samples=$samples.ToArray()}
$result | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $outputFile -Encoding utf8
Write-Output ('Resource samples saved: '+$samples.Count)
