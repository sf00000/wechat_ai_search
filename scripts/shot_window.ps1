# Capture a specific top-level window via PrintWindow (works when occluded).
param([string]$TitlePattern = "微信话题搜索下载器")

Add-Type -AssemblyName System.Drawing
Add-Type -Name U32 -Namespace W -MemberDefinition @'
[DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
[DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int n);
[DllImport("user32.dll")] public static extern bool PrintWindow(IntPtr h, IntPtr dc, uint flags);
[DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
public struct RECT { public int L, T, R, B; }
'@

$procs = Get-Process python, wechat-topic-searcher -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowTitle -like "*$TitlePattern*" }
$proc = $procs | Select-Object -First 1
if (-not $proc) { Write-Output "no window matching $TitlePattern"; exit 1 }

$h = $proc.MainWindowHandle
[W.U32]::ShowWindow($h, 9) | Out-Null
[W.U32]::SetForegroundWindow($h) | Out-Null
Start-Sleep -Milliseconds 1200

$r = New-Object W.RECT
[W.U32]::GetWindowRect($h, [ref]$r) | Out-Null
$wd = $r.R - $r.L; $ht = $r.B - $r.T
if ($wd -le 0 -or $ht -le 0) { Write-Output "bad rect: $wd x $ht"; exit 1 }

$bmp = New-Object System.Drawing.Bitmap $wd, $ht
$g = [System.Drawing.Graphics]::FromImage($bmp)
$dc = $g.GetHdc()
# PW_RENDERFULLCONTENT = 2 (捕获 GPU 合成内容)
$ok = [W.U32]::PrintWindow($h, $dc, 2)
$g.ReleaseHdc($dc)
$g.Dispose()

$out = Join-Path $PSScriptRoot 'ui_v02.png'
$bmp.Save($out, [System.Drawing.Imaging.ImageFormat]::Png)
$bmp.Dispose()
Write-Output "ok=$ok rect=${wd}x${ht} saved=$out"
