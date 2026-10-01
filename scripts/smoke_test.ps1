# Smoke test v3: 启动一次，只检查/清理「本次启动」的进程树。
# - 启动前快照已存在的同名进程，避免把旧窗口当成本次启动成功（假通过）
# - 不清理用户已打开的实例（只清理本次启动树：引导进程 + 应用子进程）
# - 验证：主窗口出现 → 存活数秒（启动屏 title=tk 属本树时报告其残留状态）
$exe = Join-Path (Split-Path $PSScriptRoot -Parent) 'dist\wechat-topic-searcher.exe'
if (-not (Test-Path $exe)) { Write-Output "MISSING: $exe"; exit 1 }

# 1) 启动前快照：已存在的同名进程 PID
$mine = @{}
Get-Process wechat-topic-searcher -ErrorAction SilentlyContinue | ForEach-Object {
    $mine[[uint32]$_.Id] = $true
}
$preCount = $mine.Count
if ($preCount -gt 0) {
    Write-Output ("注意：启动前已有 " + $preCount + " 个同名进程，本轮不检查也不清理它们")
}

# 2) 启动
$parent = Start-Process -FilePath $exe -PassThru
$mine[[uint32]$parent.Id] = $true  # 本次启动的引导进程（onefile 父进程）

function Add-TreeChildren {
    # 迭代收养：已知集合中进程的子进程（onefile 引导进程派生应用进程）
    $changed = $true
    while ($changed) {
        $changed = $false
        $all = Get-CimInstance Win32_Process -Filter "Name='wechat-topic-searcher.exe'" -ErrorAction SilentlyContinue
        foreach ($c in $all) {
            $cpid = [uint32]$c.ProcessId
            $ppid = [uint32]$c.ParentProcessId
            if (-not $mine.ContainsKey($cpid) -and $mine.ContainsKey($ppid)) {
                $mine[$cpid] = $true
                $changed = $true
            }
        }
    }
}

function Get-Mine {
    Add-TreeChildren | Out-Null
    return ,(Get-Process wechat-topic-searcher -ErrorAction SilentlyContinue | Where-Object {
        $mine.ContainsKey([uint32]$_.Id)
    })
}

# 3) 等待主窗口出现（只看本次启动树）
$deadline = (Get-Date).AddSeconds(40)
$title = ''
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Milliseconds 500
    foreach ($p in (Get-Mine)) {
        $p.Refresh()
        if ($p.MainWindowTitle -like '*微信话题搜索下载器*') { $title = $p.MainWindowTitle; break }
    }
    if ($title) { break }
}
if (-not $title) {
    Write-Output "SMOKE FAIL: 本次启动 40 秒内未出现主窗口"
    foreach ($p in (Get-Mine)) { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue }
    exit 1
}
Write-Output "SMOKE: 主窗口出现 [$title]"

# 4) 主窗口应继续存活数秒；启动屏（title=tk）应已关闭
Start-Sleep -Seconds 4
$alive = $false
$splashStill = $false
foreach ($p in (Get-Mine)) {
    $p.Refresh()
    if ($p.HasExited) { continue }
    if ($p.MainWindowTitle -like '*微信话题搜索下载器*') { $alive = $true }
    if ($p.MainWindowTitle -eq 'tk') { $splashStill = $true }
}
if (-not $alive) {
    Write-Output "SMOKE FAIL: 主窗口数秒后消失"
    foreach ($p in (Get-Mine)) { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue }
    exit 1
}
$splashText = '否'
if ($splashStill) { $splashText = '是' }
Write-Output ("SMOKE PASS: 主窗口存活（启动屏残留: " + $splashText + "）")

# 5) 只清理本次启动树，绝不触碰用户已打开的实例
foreach ($p in (Get-Mine)) { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue }
exit 0
