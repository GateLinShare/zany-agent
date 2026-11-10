<# zany 单文件二进制构建(PowerShell 版, 对应 build.sh): 产物 → release/dist/zany-<target>.exe
用法: powershell -ExecutionPolicy Bypass -File release\build.ps1 [target]
  无参 = win32-x64(本脚本只跑 Windows); target 仅决定产物命名(跨平台构建需 CI):
  linux-x64 | linux-arm64 | darwin-arm64 | darwin-x64 | win32-x64
#>
param([string]$Target = "win32-x64")
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")
$DIST = "release/dist"; $WORK = "release/build"
New-Item -ItemType Directory -Force -Path $DIST, $WORK | Out-Null

$version = uv run python -c "import tomllib;print(tomllib.load(open('pyproject.toml','rb'))['project']['version'])"

uv sync   # dev 组带 pyinstaller
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
uv run pyinstaller --onefile --name zany `
  --distpath "$DIST" --workpath "$WORK" `
  --collect-all textual --collect-all croniter `
  --add-data "styles;styles" --add-data "skills;skills" `
  --add-data "env.example;." `
  core/tui.py
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Move-Item "$DIST/zany.exe" "$DIST/zany-$Target.exe" -Force
Remove-Item zany.spec -ErrorAction SilentlyContinue  # pyinstaller 命令行模式副产物, 无用
Write-Output "built $DIST/zany-$Target.exe (v$version)"
