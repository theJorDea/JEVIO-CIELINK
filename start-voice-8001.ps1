# Local development only. A key is optional (press Enter for offline mode).
$ErrorActionPreference = 'Stop'
Push-Location $PSScriptRoot
$previous = $env:POLZA_API_KEY
try {
    if (-not $env:POLZA_API_KEY) {
        $secret = Read-Host 'Polza API key (Enter for offline mode)' -AsSecureString
        if ($secret.Length) {
            $env:POLZA_API_KEY = [System.Net.NetworkCredential]::new('', $secret).Password
        }
    }
    & .\.venv\Scripts\python.exe -m uvicorn cielink.api:app --host 127.0.0.1 --port 8001
} finally {
    $env:POLZA_API_KEY = $previous
    Pop-Location
}
