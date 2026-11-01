<#
  Windows 打包脚本：生成免安装 ZIP。

  用法（项目根目录）：
      powershell -ExecutionPolicy Bypass -File packaging/build_windows.ps1
      或双击 packaging/build_windows.bat

  产物：packaging/dist/方块兵棋-windows-x64.zip
#>
param(
    [switch]$SkipPortable
)

$ErrorActionPreference = "Stop"
# 控制台输出编码设为 UTF-8，保证中文日志在 CI 日志里正常显示
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

Write-Host "==> 1/5 检查依赖" -ForegroundColor Cyan
python --version
python -m pip install --upgrade pip | Out-Null
python -m pip install -r requirements.txt
python -m pip install pyinstaller

Write-Host "==> 2/5 生成图标" -ForegroundColor Cyan
python tools/make_icon.py | Out-Null

Write-Host "==> 3/5 PyInstaller 打包" -ForegroundColor Cyan
python -m PyInstaller packaging/wargame.spec --noconfirm --clean

Write-Host "==> 4/5 收集产物" -ForegroundColor Cyan
$Dist = Join-Path $Root "packaging\dist"
New-Item -ItemType Directory -Force -Path $Dist | Out-Null
$Target = Join-Path $Dist "BlockWargame"
if (-not (Test-Path $Target)) { throw "未找到打包产物：$Target" }

# 附带使用说明与许可证
Copy-Item README.md $Target -ErrorAction SilentlyContinue
Copy-Item GAME_GUIDE.md $Target -ErrorAction SilentlyContinue

Write-Host "==> 5/5 压缩" -ForegroundColor Cyan
$Zip = Join-Path $Dist "方块兵棋-windows-x64.zip"
if (Test-Path $Zip) { Remove-Item $Zip }
Compress-Archive -Path (Join-Path $Dist "BlockWargame") -DestinationPath $Zip -CompressionLevel Optimal

if (-not $SkipPortable) {
    Write-Host "==> 附加：便携版与源码包" -ForegroundColor Cyan
    python packaging/make_portable.py
}

Write-Host ""
Write-Host "打包完成：" -ForegroundColor Green
Get-ChildItem $Dist | ForEach-Object {
    $size = [math]::Round($_.Length / 1MB, 2)
    Write-Host ("  {0,-42} {1} MB" -f $_.Name, $size)
}
