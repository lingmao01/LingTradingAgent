$ErrorActionPreference = 'Stop'

. (Join-Path $PSScriptRoot 'process-utils.ps1')

$commandPrompt = Join-Path $env:SystemRoot 'System32\cmd.exe'

Invoke-BoundedProcess `
    -FilePath $commandPrompt `
    -Arguments '/d /s /c "exit 0"' `
    -TimeoutSeconds 5 `
    -Label 'zero-exit probe'

$nonZeroObserved = $false
try {
    Invoke-BoundedProcess `
        -FilePath $commandPrompt `
        -Arguments '/d /s /c "exit 7"' `
        -TimeoutSeconds 5 `
        -Label 'non-zero probe'
}
catch {
    if ($_.Exception.Message -ne 'non-zero probe failed (exit code 7).') {
        throw
    }
    $nonZeroObserved = $true
}
if (-not $nonZeroObserved) {
    throw 'The bounded process adapter accepted a non-zero exit code.'
}

$timeoutObserved = $false
$stopwatch = [Diagnostics.Stopwatch]::StartNew()
try {
    Invoke-BoundedProcess `
        -FilePath $commandPrompt `
        -Arguments '/d /s /c "ping.exe -n 6 127.0.0.1 >NUL"' `
        -TimeoutSeconds 1 `
        -Label 'timeout probe'
}
catch {
    if ($_.Exception.Message -ne 'timeout probe timed out after 1 seconds.') {
        throw
    }
    $timeoutObserved = $true
}
finally {
    $stopwatch.Stop()
}
if (-not $timeoutObserved) {
    throw 'The bounded process adapter accepted a process that exceeded its timeout.'
}
if ($stopwatch.Elapsed.TotalSeconds -ge 12) {
    throw "The timeout probe took too long to terminate: $($stopwatch.Elapsed.TotalSeconds) seconds."
}

Write-Host 'Bounded native process exit-code and timeout handling: OK'
