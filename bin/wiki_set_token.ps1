# One-time setup for the strongest ingest engine.
#
# The scheduled task runs outside the Claude Code host app, so it has no
# credentials of its own. Generate a long-lived token and store it here; it is
# encrypted with DPAPI and can only be read back by this Windows account.
#
#   1) claude setup-token          # interactive, opens a browser, prints a token
#   2) powershell -ExecutionPolicy Bypass -File wiki_set_token.ps1
#
# Entirely optional: without a token the system falls back to a local engine.
# Remove the token with:  Remove-Item "$env:USERPROFILE\.claude\wiki-state\oauth-token.dpapi"

$ErrorActionPreference = 'Stop'

$ConfigHome = if ($env:CLAUDE_CONFIG_DIR) { $env:CLAUDE_CONFIG_DIR } else { Join-Path $env:USERPROFILE '.claude' }
$StateDir = Join-Path $ConfigHome 'wiki-state'
$TokenFile = Join-Path $StateDir 'oauth-token.dpapi'

if (-not (Test-Path $StateDir)) { New-Item -ItemType Directory -Force -Path $StateDir | Out-Null }

Write-Host 'Paste the token from `claude setup-token` (input is hidden):'
$secure = Read-Host -AsSecureString
if (-not $secure -or $secure.Length -eq 0) {
    Write-Host 'Empty input, nothing written.'
    exit 1
}

# WriteAllText, not Set-Content: no BOM and no trailing newline, either of which
# makes ConvertTo-SecureString fail on read-back.
$blob = $secure | ConvertFrom-SecureString
[IO.File]::WriteAllText($TokenFile, $blob)

# Prove the round-trip now rather than failing silently at 04:07.
try {
    $check = (Get-Content $TokenFile -Raw).Trim() | ConvertTo-SecureString
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($check)
    $len = [Runtime.InteropServices.Marshal]::PtrToStringAuto($bstr).Length
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    Write-Host "Stored and verified (DPAPI, this account only): $TokenFile  [$len chars]"
} catch {
    Remove-Item $TokenFile -ErrorAction SilentlyContinue
    Write-Host "FAILED to read the token back, nothing was kept: $($_.Exception.Message)"
    exit 1
}
