<# 把构建好的二进制装进系统 PATH(zany 命令)。会覆盖 deploy.cmd 装的源码版(uv trampoline exe, 直接覆盖)。
用法: powershell -ExecutionPolicy Bypass -File release\install.ps1 [二进制路径]
  无参 = release/dist/zany-win32-x64.exe
#>
param([string]$BinPath = "release/dist/zany-win32-x64.exe")
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")
if (-not (Test-Path $BinPath)) { Write-Error "找不到 $BinPath, 先跑 release\build.ps1"; exit 1 }

$dest = Join-Path $env:USERPROFILE ".local\bin\zany.exe"
New-Item -ItemType Directory -Force -Path (Split-Path $dest) | Out-Null
$prev = (Get-Command zany.exe -ErrorAction SilentlyContinue).Source
Copy-Item $BinPath $dest -Force
Write-Output "installed: $dest"
if ($prev -and $prev -ne $dest) { Write-Output "  (覆盖了原 $prev)" }
if (-not (Get-Command zany.exe -ErrorAction SilentlyContinue)) {
  Write-Warning "$env:USERPROFILE\.local\bin 不在 PATH, 加进 PATH 后 zany 才可用"
}
& $dest --help *> $null
if ($LASTEXITCODE -eq 0) { Write-Output "smoke: --help ok" }
