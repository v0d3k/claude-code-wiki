# Autonomous ingest runner (Windows Task Scheduler entry point).
#
# Thin wrapper around wiki_ingest.py, which picks the engine itself.
# Exits without spending anything when the queue is empty.
#
# Manual run:  powershell -ExecutionPolicy Bypass -File wiki-ingest-run.ps1
# Lint run:    powershell -ExecutionPolicy Bypass -File wiki-ingest-run.ps1 -Mode lint
# Pin engine:  powershell -ExecutionPolicy Bypass -File wiki-ingest-run.ps1 -Engine ollama

param(
    [ValidateSet('ingest', 'lint')]
    [string]$Mode = 'ingest',
    [ValidateSet('auto', 'claude', 'ollama')]
    [string]$Engine = 'auto',
    [int]$TimeoutMinutes = 45
)

$ErrorActionPreference = 'Continue'

# Everything is derived from this script's own location, so the package can be
# cloned or moved anywhere.
$BinDir = Split-Path -Parent $PSCommandPath
$Ingest = Join-Path $BinDir 'wiki_ingest.py'
$ConfigHome = if ($env:CLAUDE_CONFIG_DIR) { $env:CLAUDE_CONFIG_DIR } else { Join-Path $env:USERPROFILE '.claude' }
$StateDir = Join-Path $ConfigHome 'wiki-state'
$LogFile = Join-Path $StateDir 'ingest-runs.log'
$TokenFile = Join-Path $StateDir 'oauth-token.dpapi'

if (-not (Test-Path $StateDir)) { New-Item -ItemType Directory -Force -Path $StateDir | Out-Null }

function Write-Log([string]$msg) {
    Add-Content -Path $LogFile -Encoding utf8 -Value ("{0} {1}" -f (Get-Date -Format 'yyyy-MM-ddTHH:mm:ss'), $msg)
}

if (-not (Test-Path $Ingest)) {
    Write-Log "abort: engine script missing at $Ingest"
    exit 1
}

# Optional: a stored token upgrades the run to the claude engine. Absent is fine.
if (-not $env:CLAUDE_CODE_OAUTH_TOKEN -and -not $env:ANTHROPIC_API_KEY -and (Test-Path $TokenFile)) {
    try {
        $secure = (Get-Content $TokenFile -Raw).Trim() | ConvertTo-SecureString
        $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
        $env:CLAUDE_CODE_OAUTH_TOKEN = [Runtime.InteropServices.Marshal]::PtrToStringAuto($bstr)
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    } catch {
        Write-Log "warn: token file unreadable, continuing without it -- $($_.Exception.Message)"
    }
}

$outFile = Join-Path $StateDir 'ingest-last-run.txt'
$errFile = Join-Path $StateDir 'ingest-last-run.err'

$python = if (Get-Command py -ErrorAction SilentlyContinue) { 'py' } else { 'python' }
$argList = @()
if ($python -eq 'py') { $argList += '-3' }
$argList += @($Ingest, '--mode', $Mode, '--engine', $Engine)

$proc = Start-Process -FilePath $python -ArgumentList $argList `
    -NoNewWindow -PassThru -RedirectStandardOutput $outFile -RedirectStandardError $errFile

if (-not $proc.WaitForExit($TimeoutMinutes * 60 * 1000)) {
    try { $proc.Kill() } catch {}
    Write-Log "timeout after $TimeoutMinutes min -- killed"
    exit 1
}
$proc.WaitForExit()   # blocking form: without it ExitCode can still be unset

if ($proc.ExitCode -ne 0) {
    $err = if (Test-Path $errFile) { (Get-Content $errFile -Tail 3 | Out-String).Trim() } else { '' }
    Write-Log ("exit={0} :: {1}" -f $proc.ExitCode, ($err -replace '\s+', ' '))
}
exit $proc.ExitCode
