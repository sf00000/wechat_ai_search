# Smoke test v2: onefile exe spawns a child with the same name;
# check ALL same-name processes for a visible main window.
$exe = Join-Path (Split-Path $PSScriptRoot -Parent) 'dist\wechat-topic-searcher.exe'
if (-not (Test-Path $exe)) { Write-Output "MISSING: $exe"; exit 1 }

Start-Process -FilePath $exe | Out-Null
$deadline = (Get-Date).AddSeconds(30)
$title = ''
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Milliseconds 800
    $procs = Get-Process wechat-topic-searcher -ErrorAction SilentlyContinue
    foreach ($p in $procs) {
        $p.Refresh()
        if ($p.MainWindowTitle) { $title = $p.MainWindowTitle; break }
    }
    if ($title) { break }
}
if ($title) {
    $procs = Get-Process wechat-topic-searcher -ErrorAction SilentlyContinue
    $mem = [math]::Round((($procs | Measure-Object WorkingSet64 -Sum).Sum) / 1MB)
    Write-Output "SMOKE PASS: procs=$($procs.Count) memory=${mem}MB title=[$title]"
    $procs | Stop-Process -Force
    exit 0
}
Get-Process wechat-topic-searcher -ErrorAction SilentlyContinue | Stop-Process -Force
Write-Output "SMOKE FAIL: no window in 30s"
exit 1
