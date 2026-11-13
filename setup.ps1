# VideoForge one-time installer. Double-click Install.bat to run it.
# Safe to run again at any time (it skips whatever is already done).
$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot
$Host.UI.RawUI.WindowTitle = "VideoForge setup"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

function Step($m) { Write-Host ""; Write-Host "==> $m" -ForegroundColor Cyan }
function Ok($m)   { Write-Host "    OK  $m" -ForegroundColor Green }
function Warn($m) { Write-Host "    !!  $m" -ForegroundColor Yellow }
function Fail($m) {
    Write-Host ""; Write-Host "SETUP STOPPED: $m" -ForegroundColor Red
    Write-Host "Fix the problem above, then double-click Install.bat again."
    exit 1
}
function Refresh-Path {
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                [Environment]::GetEnvironmentVariable("Path", "User")
}
function Has-Winget { [bool](Get-Command winget -ErrorAction SilentlyContinue) }
function Write-Utf8($path, $text) {
    [IO.File]::WriteAllText((Join-Path $PSScriptRoot $path), $text, (New-Object Text.UTF8Encoding $false))
}

Write-Host "VideoForge setup" -ForegroundColor White
Write-Host "Takes about 5-15 minutes and downloads about 1.5 GB. Keep this window open."

# ---------------------------------------------------------------- disk space
$drive = (Get-Item $PSScriptRoot).PSDrive
$freeGB = [math]::Round($drive.Free / 1GB, 1)
if ($freeGB -lt 6) {
    Warn "Only $freeGB GB free on drive $($drive.Name): - VideoForge needs at least 6 GB (10+ recommended)."
    Read-Host "Press Enter to continue anyway, or close this window to stop"
}

# ---------------------------------------------------------------- Python
Step "Checking for Python 3.11 - 3.14"
function Find-Python {
    $cands = @()
    foreach ($v in "3.12", "3.13", "3.14", "3.11") { $cands += , @("py", "-$v") }
    foreach ($d in "Python312", "Python313", "Python314", "Python311") {
        $cands += , @("$env:LOCALAPPDATA\Programs\Python\$d\python.exe")
        $cands += , @("$env:ProgramFiles\Python\$d\python.exe")
        $cands += , @("$env:ProgramFiles\$d\python.exe")
    }
    $cands += , @("python")
    foreach ($c in $cands) {
        $exe = $c[0]
        if ($exe -like "*\*" -and -not (Test-Path $exe)) { continue }
        if ($exe -notlike "*\*" -and -not (Get-Command $exe -ErrorAction SilentlyContinue)) { continue }
        $rest = @($c | Select-Object -Skip 1)
        try {
            $out = & $exe @rest -c "import sys; print(sys.executable); print('%d.%d' % sys.version_info[:2])" 2>$null
        } catch { continue }
        if ($LASTEXITCODE -eq 0 -and $out.Count -ge 2) {
            $ver = [version]$out[1]
            if ($ver -ge [version]"3.11" -and $ver -le [version]"3.14") { return $out[0] }
        }
    }
    return $null
}

$py = Find-Python
if (-not $py) {
    if (-not (Has-Winget)) {
        Start-Process "https://www.python.org/downloads/release/python-31210/"
        Fail ("Python is not installed and this PC has no 'winget' to install it automatically. " +
              "Install Python 3.12 from the page that just opened (tick 'Add python.exe to PATH').")
    }
    Write-Host "    Python not found - installing Python 3.12 (just for your user, no admin needed)..."
    winget install -e --id Python.Python.3.12 --scope user --silent `
        --accept-package-agreements --accept-source-agreements `
        --override "/quiet InstallAllUsers=0 PrependPath=1 Include_launcher=1 Include_test=0"
    Refresh-Path
    $py = Find-Python
    if (-not $py) { Fail "Python install did not complete. Restart the PC and run Install.bat again." }
}
Ok "Python: $py"

# ---------------------------------------------------------------- venv + packages
Step "Installing VideoForge's Python packages (the longest step)"
$venvPy = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (Test-Path $venvPy) {
    & $venvPy -c "import sys" 2>$null
    if ($LASTEXITCODE -ne 0) { Warn "Existing .venv is broken - recreating"; Remove-Item -Recurse -Force .venv }
}
if (-not (Test-Path $venvPy)) {
    & $py -m venv .venv
    if ($LASTEXITCODE -ne 0) { Fail "Could not create the Python environment (.venv)." }
}
& $venvPy -m pip install --upgrade pip --disable-pip-version-check -q
& $venvPy -m pip install -r requirements.txt -c constraints.txt --disable-pip-version-check
if ($LASTEXITCODE -ne 0) { Fail "Package install failed (check your internet connection)." }
# the YouTube downloader must always be the newest release (YouTube changes often)
& $venvPy -m pip install --upgrade "yt-dlp[default]" --disable-pip-version-check -q
Ok "Packages installed"

# ---------------------------------------------------------------- Deno (YouTube downloads)
Step "Checking Deno (yt-dlp needs it for reliable YouTube downloads)"
Refresh-Path
if (Get-Command deno -ErrorAction SilentlyContinue) {
    Ok "Deno already installed"
} elseif (Has-Winget) {
    winget install -e --id DenoLand.Deno --silent --accept-package-agreements --accept-source-agreements
    Refresh-Path
    if (Get-Command deno -ErrorAction SilentlyContinue) { Ok "Deno installed" }
    else { Warn "Deno install may need a PC restart to take effect. Local video files work regardless." }
} else {
    Warn "Could not install Deno automatically (no winget). Some YouTube links may fail to download; local files work."
}

# ---------------------------------------------------------------- settings + AI key
Step "AI settings"
if (-not (Test-Path .env)) { Copy-Item .env.example .env; Ok "Created .env settings file" }
$envText = [IO.File]::ReadAllText((Join-Path $PSScriptRoot ".env"))

function Set-EnvLine($text, $key, $value) {
    $line = "$key=$value"
    if ($text -match "(?m)^#?[ 	]*$key=.*$") { return [regex]::Replace($text, "(?m)^#?[ 	]*$key=.*$", $line, 1) }
    return $text.TrimEnd() + "`r`n$line`r`n"
}

$hasKey = $envText -match "(?m)^GROQ_API_KEY=[ 	]*[^\s#]+"
if ($hasKey) {
    Ok "Groq AI key already set"
} else {
    Write-Host ""
    Write-Host "    VideoForge uses a FREE AI (Groq) to pick the best moments and write titles."
    Write-Host "    You need a free key: sign in at console.groq.com -> 'Create API Key' -> copy it."
    Write-Host "    (You can skip this - clips still get made, just less cleverly.)"
    $ans = Read-Host "    Open the Groq website now? (Y/n)"
    if ($ans -notmatch "^[nN]") { Start-Process "https://console.groq.com/keys" }
    for ($try = 1; $try -le 3; $try++) {
        $key = (Read-Host "    Paste your Groq key (starts with gsk_) and press Enter, or just press Enter to skip").Trim()
        if (-not $key) { Warn "Skipped. You can add GROQ_API_KEY to the .env file later."; break }
        try {
            $models = Invoke-RestMethod "https://api.groq.com/openai/v1/models" `
                -Headers @{ Authorization = "Bearer $key" } -TimeoutSec 20
        } catch {
            Warn "That key did not work ($($_.Exception.Message)). Try again."
            continue
        }
        $ids = @($models.data | ForEach-Object { $_.id })
        $envText = Set-EnvLine $envText "GROQ_API_KEY" $key
        $want = "llama-3.1-8b-instant"
        if ($ids -notcontains $want) {
            $alt = @($ids | Where-Object { $_ -match "llama" -and $_ -match "instant|8b" }) +
                   @($ids | Where-Object { $_ -match "llama" }) + $ids | Select-Object -First 1
            if ($alt) { $envText = Set-EnvLine $envText "GROQ_MODEL" $alt; Ok "Using Groq model $alt" }
        }
        Write-Utf8 ".env" $envText
        Ok "Groq key saved"
        break
    }
}

# ---------------------------------------------------------------- speech-to-text model
Step "Downloading the speech-to-text model (~0.5 GB, used for videos without captions)"
& $venvPy -c "from backend import transcribe; transcribe._model(); print('ready')"
if ($LASTEXITCODE -eq 0) { Ok "Speech-to-text ready" }
else { Warn "Model download failed - it will be retried automatically the first time it is needed." }

# ---------------------------------------------------------------- desktop shortcut
Step "Creating a desktop shortcut"
try {
    $desktop = [Environment]::GetFolderPath("Desktop")
    $sc = (New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path $desktop "VideoForge.lnk"))
    $sc.TargetPath = Join-Path $PSScriptRoot "Start VideoForge.bat"
    $sc.WorkingDirectory = $PSScriptRoot
    $sc.IconLocation = "$env:SystemRoot\System32\imageres.dll,18"
    $sc.Save()
    Ok "Desktop shortcut 'VideoForge' created"
} catch { Warn "Could not create the shortcut - use 'Start VideoForge.bat' in this folder instead." }

Write-Host ""
Write-Host "All done! Start VideoForge any time from the desktop shortcut." -ForegroundColor Green
$ans = Read-Host "Start it now? (Y/n)"
if ($ans -notmatch "^[nN]") { Start-Process (Join-Path $PSScriptRoot "Start VideoForge.bat") -WorkingDirectory $PSScriptRoot }
