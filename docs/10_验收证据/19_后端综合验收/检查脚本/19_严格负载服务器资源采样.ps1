[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$OutputPath,

    [ValidateRange(10, 86400)]
    [int]$DurationSeconds = 1200,

    [ValidateRange(1000, 10000)]
    [int]$IntervalMilliseconds = 1000
)

$ErrorActionPreference = 'Stop'
$fullPath = [System.IO.Path]::GetFullPath($OutputPath)
$directory = [System.IO.Path]::GetDirectoryName($fullPath)
if (-not [System.IO.Directory]::Exists($directory)) {
    throw "Output directory does not exist: $directory"
}

$diskClass = Get-CimClass -Namespace root/cimv2 -ClassName Win32_PerfFormattedData_PerfDisk_PhysicalDisk -ErrorAction Stop
$processorClass = Get-CimClass -Namespace root/cimv2 -ClassName Win32_PerfFormattedData_PerfOS_Processor -ErrorAction Stop
$memoryClass = Get-CimClass -Namespace root/cimv2 -ClassName Win32_PerfFormattedData_PerfOS_Memory -ErrorAction Stop

function Get-CounterDisplayName($class, [string]$propertyName) {
    $qualifier = $class.CimClassProperties[$propertyName].Qualifiers |
        Where-Object Name -eq 'DisplayName' | Select-Object -First 1
    if (-not $qualifier) { throw "Missing localized counter name: $($class.CimClassName).$propertyName" }
    return [string]$qualifier.Value
}

$diskSetName = [string](($diskClass.CimClassQualifiers | Where-Object Name -eq 'DisplayName' | Select-Object -First 1).Value)
$processorSetName = [string](($processorClass.CimClassQualifiers | Where-Object Name -eq 'DisplayName' | Select-Object -First 1).Value)
$memorySetName = [string](($memoryClass.CimClassQualifiers | Where-Object Name -eq 'DisplayName' | Select-Object -First 1).Value)
$counterDefinitions = @{}
$counterPaths = @()

foreach ($definition in @(
    @{ class = $diskClass; property = 'DiskReadsPersec'; field = 'read_iops'; source = 'disk' },
    @{ class = $diskClass; property = 'DiskWritesPersec'; field = 'write_iops'; source = 'disk' },
    @{ class = $diskClass; property = 'DiskReadBytesPersec'; field = 'read_bytes_per_second'; source = 'disk' },
    @{ class = $diskClass; property = 'DiskWriteBytesPersec'; field = 'write_bytes_per_second'; source = 'disk' },
    @{ class = $diskClass; property = 'AvgDisksecPerRead'; field = 'average_read_latency_ms'; source = 'disk'; scale = 1000 },
    @{ class = $diskClass; property = 'AvgDisksecPerWrite'; field = 'average_write_latency_ms'; source = 'disk'; scale = 1000 },
    @{ class = $diskClass; property = 'AvgDisksecPerTransfer'; field = 'average_latency_ms'; source = 'disk'; scale = 1000 },
    @{ class = $diskClass; property = 'CurrentDiskQueueLength'; field = 'queue_length'; source = 'disk' },
    @{ class = $diskClass; property = 'PercentDiskTime'; field = 'busy_percent'; source = 'disk' },
    @{ class = $processorClass; property = 'PercentProcessorTime'; field = 'host_cpu_percent'; source = 'cpu' },
    @{ class = $memoryClass; property = 'AvailableBytes'; field = 'available_memory_bytes'; source = 'memory' }
)) {
    $setName = switch ($definition.class.CimClassName) {
        'Win32_PerfFormattedData_PerfDisk_PhysicalDisk' { $diskSetName }
        'Win32_PerfFormattedData_PerfOS_Processor' { $processorSetName }
        default { $memorySetName }
    }
    $counterName = Get-CounterDisplayName $definition.class $definition.property
    $instance = if ($definition.source -eq 'disk') { '(*)' } elseif ($definition.source -eq 'cpu') { '(_Total)' } else { '' }
    $counterPaths += "\$setName$instance\$counterName"
    $counterDefinitions[$counterName] = $definition
}

# Fail before creating the artifact if required Windows counters are unavailable.
$watch = [System.Diagnostics.Stopwatch]::StartNew()
$preflight = @(Get-Counter -Counter $counterPaths -SampleInterval ($IntervalMilliseconds / 1000) `
    -MaxSamples 2 -ErrorAction Stop)
if ($preflight.Count -lt 2 -or $preflight[-1].CounterSamples.Count -lt $counterPaths.Count) {
    throw 'Required PhysicalDisk, Processor, or Memory performance counters are unavailable.'
}
$latestSampleSet = $preflight[-1]

$file = [System.IO.File]::Open(
    $fullPath,
    [System.IO.FileMode]::CreateNew,
    [System.IO.FileAccess]::Write,
    [System.IO.FileShare]::Read
)
$writer = [System.IO.StreamWriter]::new($file, [System.Text.UTF8Encoding]::new($false))
$sampleCount = 0

try {
    while ($watch.Elapsed.TotalSeconds -lt $DurationSeconds) {
        $sample = [ordered]@{
            timestamp_utc = [DateTime]::UtcNow.ToString('o')
            elapsed_seconds = [Math]::Round($watch.Elapsed.TotalSeconds, 3)
            sampling_error = $null
            host_cpu_percent = $null
            available_memory_bytes = $null
            physical_disks = @()
            processes = @()
        }

        try {
            if ($null -eq $latestSampleSet) {
                $sampleSet = @(Get-Counter -Counter $counterPaths `
                    -SampleInterval ($IntervalMilliseconds / 1000) -MaxSamples 2 -ErrorAction Stop)[-1]
            }
            else {
                $sampleSet = $latestSampleSet
                $latestSampleSet = $null
            }
            $sample.timestamp_utc = $sampleSet.Timestamp.ToUniversalTime().ToString('o')
            $diskMap = @{}
            foreach ($counter in $sampleSet.CounterSamples) {
                $counterName = ($counter.Path -split '\\')[-1]
                $definition = $counterDefinitions[$counterName]
                if (-not $definition) { continue }
                $value = [double]$counter.CookedValue
                if ($definition.scale) { $value *= [double]$definition.scale }
                if ($definition.source -eq 'disk') {
                    $instance = [string]$counter.InstanceName
                    if (-not $diskMap.ContainsKey($instance)) { $diskMap[$instance] = @{} }
                    $diskMap[$instance][$definition.field] = $value
                }
                elseif ($definition.source -eq 'cpu') {
                    $sample.host_cpu_percent = $value
                }
                elseif ($definition.source -eq 'memory') {
                    $sample.available_memory_bytes = [long]$value
                }
            }
            $sample.physical_disks = @($diskMap.GetEnumerator() | ForEach-Object {
                [ordered]@{ instance = $_.Key } + $_.Value
            })
            $processes = Get-Process -ErrorAction Stop | Where-Object {
                $_.ProcessName -in @('python', 'mysqld', 'caddy')
            }

            $sample.processes = @($processes | ForEach-Object {
                [ordered]@{
                    pid = [int]$_.Id
                    name = $_.ProcessName
                    cpu_cumulative_seconds = if ($null -ne $_.CPU) { [double]$_.CPU } else { $null }
                    working_set_bytes = [long]$_.WorkingSet64
                    thread_count = [int]$_.Threads.Count
                }
            })
        }
        catch {
            $sample.sampling_error = $_.Exception.GetType().FullName
        }

        $writer.WriteLine(($sample | ConvertTo-Json -Depth 6 -Compress))
        $writer.Flush()
        $sampleCount++

    }
}
finally {
    $watch.Stop()
    $writer.Dispose()
}

Write-Output ([ordered]@{
    output_path = $fullPath
    duration_seconds = [Math]::Round($watch.Elapsed.TotalSeconds, 3)
    interval_milliseconds = $IntervalMilliseconds
    sample_count = $sampleCount
    note = 'Read-only Windows counters; timestamps are measured values and actual gaps may exceed the configured sample interval.'
} | ConvertTo-Json -Compress)
