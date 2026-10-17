$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$model = 'qwen3.5:4b'

try {
    $ollama = Get-Command ollama -ErrorAction Stop
} catch {
    Write-Error 'Ollama is not installed. Install it from https://ollama.com/download/windows, then run this script again.'
    exit 1
}

try {
    $tags = Invoke-RestMethod -Uri 'http://127.0.0.1:11434/api/tags' -TimeoutSec 3
} catch {
    Write-Host 'Starting the local Ollama service...'
    Start-Process -FilePath $ollama.Source -ArgumentList 'serve' -WindowStyle Hidden
    $tags = $null
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        Start-Sleep -Seconds 2
        try {
            $tags = Invoke-RestMethod -Uri 'http://127.0.0.1:11434/api/tags' -TimeoutSec 3
            break
        } catch { }
    }
    if (-not $tags) {
        Write-Error 'Ollama did not start. Open the Ollama app from the Windows Start menu, then run this script again.'
        exit 1
    }
}

if (-not ($tags.models | Where-Object { $_.name -eq $model })) {
    Write-Host "Downloading $model (about 3.3–4.0 GB)..."
    & $ollama.Source pull $model
    if ($LASTEXITCODE -ne 0) {
        Write-Error "Ollama could not download $model. Check your internet connection and available disk space, then run this script again."
        exit 1
    }
} else {
    Write-Host "$model is already installed."
}

$tags = Invoke-RestMethod -Uri 'http://127.0.0.1:11434/api/tags' -TimeoutSec 5
if (-not ($tags.models | Where-Object { $_.name -eq $model })) {
    Write-Error "Ollama did not report $model after setup. Run 'ollama list' to inspect installed models."
    exit 1
}

Write-Host "Ollama is ready with $model at http://127.0.0.1:11434."
Write-Host 'Return to SiMi, reload the page, and retry the failed video.'
$assets = Get-ChildItem -LiteralPath '.\data\assets' -File -Recurse -ErrorAction SilentlyContinue
if (-not $assets) {
    Write-Warning 'No local video assets were found. Generation will need licensed clips under data/assets/<keyword>/ or data/assets/fallback/ after the script model is ready.'
}
