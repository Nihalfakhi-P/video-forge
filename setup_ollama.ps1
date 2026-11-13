# One-time Ollama setup for VideoForge.
# Needs ~5 GB free on C: (app ~2 GB + llama3.2 model ~2 GB).
$free = [math]::Round((Get-PSDrive C).Free/1GB, 1)
Write-Host "Free space on C: $free GB"
if ($free -lt 5) {
    Write-Host "Not enough free space (need ~5 GB)." -ForegroundColor Yellow
    Write-Host "Free up space on C: (Downloads, Videos, OneDrive 'Free up space'), or use the"
    Write-Host "free Groq key instead (GROQ_API_KEY in .env) - it needs no disk space."
    exit 1
}
winget install -e --id Ollama.Ollama --accept-source-agreements --accept-package-agreements
# refresh PATH in this session
$env:Path = [Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [Environment]::GetEnvironmentVariable("Path","User")
ollama pull llama3.2
Write-Host "Done. Restart VideoForge (run.ps1) - the LLM pill should turn green." -ForegroundColor Green
