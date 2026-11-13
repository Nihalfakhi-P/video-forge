# Start VideoForge (normally via "Start VideoForge.bat")
Set-Location $PSScriptRoot
# pick up tools installed since this window's parent started (e.g. Deno)
$env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
            [Environment]::GetEnvironmentVariable("Path", "User")
& "$PSScriptRoot\.venv\Scripts\python.exe" -m uvicorn backend.main:app --host 127.0.0.1 --port 8321
