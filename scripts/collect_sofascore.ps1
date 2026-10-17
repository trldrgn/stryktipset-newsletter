# Collect this week's Sofascore snapshot (xG + missing players) and push it,
# so the GitHub newsletter run can use it. Sofascore blocks GitHub runners,
# so this runs locally via Windows Task Scheduler (Fri evening + Sat morning).
#
# Manual run:  powershell -NoProfile -ExecutionPolicy Bypass -File scripts\collect_sofascore.ps1

$ErrorActionPreference = "Continue"
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

$logDir = Join-Path $repo "data\logs"
New-Item -ItemType Directory -Force $logDir | Out-Null
$log = Join-Path $logDir "sofascore_collect.log"

function Log($msg) {
    "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $msg" | Tee-Object -FilePath $log -Append
}

Log "=== Sofascore collection started ==="

git pull --rebase --autostash origin main *>> $log
if ($LASTEXITCODE -ne 0) { Log "git pull failed (exit $LASTEXITCODE) — continuing with local copy" }

$env:PYTHONIOENCODING = "utf-8"
& "$repo\.venv\Scripts\python.exe" main.py --collect-sofascore *>> $log
if ($LASTEXITCODE -ne 0) {
    Log "Collector failed (exit $LASTEXITCODE) — nothing pushed"
    exit 1
}

git add data/sofascore *>> $log
git diff --staged --quiet
if ($LASTEXITCODE -eq 0) {
    Log "Snapshot unchanged — nothing to commit"
    exit 0
}

$draw = (Get-ChildItem data\sofascore\draw_*.json | Sort-Object LastWriteTime -Descending | Select-Object -First 1).BaseName
git commit -m "data: sofascore snapshot $draw ($(Get-Date -Format 'yyyy-MM-dd HH:mm'))" *>> $log

for ($i = 1; $i -le 3; $i++) {
    git pull --rebase --autostash origin main *>> $log
    git push origin HEAD:main *>> $log
    if ($LASTEXITCODE -eq 0) { Log "Snapshot pushed ($draw)"; exit 0 }
    Log "Push attempt $i failed — retrying in 10s"
    Start-Sleep -Seconds 10
}
Log "Push failed after 3 attempts"
exit 1
