$ErrorActionPreference = 'Stop'
Push-Location $PSScriptRoot
$previous = $env:POLZA_API_KEY
try {
    if (-not $env:POLZA_API_KEY) {
        $secret = Read-Host 'Polza API key' -AsSecureString
        if (-not $secret.Length) { throw 'API key required for Gemini dialogue.' }
        $env:POLZA_API_KEY = [System.Net.NetworkCredential]::new('', $secret).Password
    }
    & .\.venv\Scripts\python.exe -m uvicorn cielink.gemini_trial:app --host 127.0.0.1 --port 8011
} finally {
    $env:POLZA_API_KEY = $previous
    Pop-Location
}
