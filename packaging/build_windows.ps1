<#
  Windows 打包脚本：生成免安装 ZIP。

  用法（项目根目录）：
      powershell -ExecutionPolicy Bypass -File packaging/build_windows.ps1
      或双击 packaging/build_windows.bat

  产物：packaging/dist/fangkuai-bingqi-windows-x64.zip

  注意（重要）：
  Windows PowerShell 5.1 会按系统 ANSI 代码页读取没有 BOM 的 .ps1 文件。
  在英文环境（cp1252）下中文字符串会被误解码，某些字节还会被 PowerShell
  当成它认可的引号（U+201D），导致字符串提前闭合、整个脚本解析失败。
  因此本文件的**可执行内容一律使用 ASCII**，中文只出现在注释里；
  同时写入 UTF-8 BOM，保证注释在各类代码页下都能正确显示。
#>
param(
    [switch]$SkipPortable
)

$ErrorActionPreference = "Stop"
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$PyiDist = Join-Path $Root "packaging\dist"
$PyiBuild = Join-Path $Root "packaging\build"
$Dist = $PyiDist

Write-Host "==> 1/5 check dependencies" -ForegroundColor Cyan
python --version
python -m pip install --upgrade pip | Out-Null
python -m pip install -r requirements.txt
python -m pip install pyinstaller

Write-Host "==> 2/5 generate icons" -ForegroundColor Cyan
python tools/make_icon.py | Out-Null

Write-Host "==> 3/5 build with PyInstaller" -ForegroundColor Cyan
python -m PyInstaller packaging/wargame.spec --noconfirm --clean --distpath $PyiDist --workpath $PyiBuild

$Target = Join-Path $PyiDist "BlockWargame"
if (-not (Test-Path $Target)) { throw "packaged output not found: $Target" }

Write-Host "==> 4/5 verify packaged executable" -ForegroundColor Cyan
$Exe = Join-Path $Target "BlockWargame.exe"
if (-not (Test-Path $Exe)) { throw "executable not found: $Exe" }
# GUI 程序没有控制台，--check 的结果以退出码为准（0 = 数据校验通过）
$proc = Start-Process -FilePath $Exe -ArgumentList "--check" -Wait -PassThru
if ($proc.ExitCode -ne 0) { throw "packaged self-check failed with exit code $($proc.ExitCode)" }
Write-Host "    packaged self-check passed"

Copy-Item README.md $Target -ErrorAction SilentlyContinue
Copy-Item GAME_GUIDE.md $Target -ErrorAction SilentlyContinue
Copy-Item LICENSE $Target -ErrorAction SilentlyContinue

Write-Host "==> 5/5 create archive" -ForegroundColor Cyan
New-Item -ItemType Directory -Force -Path $Dist | Out-Null
$Zip = Join-Path $Dist "fangkuai-bingqi-windows-x64.zip"
if (Test-Path $Zip) { Remove-Item $Zip }
Compress-Archive -Path $Target -DestinationPath $Zip -CompressionLevel Optimal

if (-not $SkipPortable) {
    Write-Host "==> extra: portable build and source archive" -ForegroundColor Cyan
    python packaging/make_portable.py
}

Write-Host ""
Write-Host "Build finished:" -ForegroundColor Green
Get-ChildItem $Dist | ForEach-Object {
    $size = [math]::Round($_.Length / 1MB, 2)
    Write-Host ("  {0,-42} {1} MB" -f $_.Name, $size)
}
