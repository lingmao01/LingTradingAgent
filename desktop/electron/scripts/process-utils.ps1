function Invoke-BoundedProcess {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$FilePath,
        [Parameter(Mandatory)][string]$Arguments,
        [Parameter(Mandatory)][ValidateRange(1, 2147483)][int]$TimeoutSeconds,
        [Parameter(Mandatory)][string]$Label
    )

    # Windows PowerShell 5.1 can return a Process object with a null ExitCode
    # for Start-Process -PassThru -NoNewWindow (PowerShell/PowerShell#20400).
    # Use the .NET process API directly so timeout and exit-code evidence both
    # come from the same live process handle on every supported Windows image.
    $startInfo = [Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = $FilePath
    $startInfo.Arguments = $Arguments
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true

    $process = [Diagnostics.Process]::new()
    $process.StartInfo = $startInfo
    try {
        if (-not $process.Start()) {
            throw "$Label failed to start."
        }
        if (-not $process.WaitForExit($TimeoutSeconds * 1000)) {
            $taskkill = Join-Path $env:SystemRoot 'System32\taskkill.exe'
            & $taskkill /PID $process.Id /T /F | Out-Null
            $process.WaitForExit(10000) | Out-Null
            throw "$Label timed out after $TimeoutSeconds seconds."
        }

        # The parameterless wait flushes final process state before ExitCode is
        # read. It returns immediately because the bounded wait already ended.
        $process.WaitForExit()
        $exitCode = [int]$process.ExitCode
        if ($exitCode -ne 0) {
            throw "$Label failed (exit code $exitCode)."
        }
    }
    finally {
        $process.Dispose()
    }
}
