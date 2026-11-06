@echo off
rem zany 一键部署(Windows 原生版, 对应 deploy.sh): uv(缺则提示装) + uv sync + 全局命令 zany
rem 用法: 仓库根 cmd/PowerShell 跑 deploy.cmd (fresh clone 即可, python/依赖由 uv 管理)
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set "REPO=%CD%"

rem [1/3] uv: 没有就提示装(官方安装器, 落 %USERPROFILE%\.local\bin)
where uv >nul 2>nul
if errorlevel 1 (
  echo [1/3] 未找到 uv, 请先安装再重跑本脚本:
  echo   powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
  exit /b 1
)
echo [1/3] uv 已有

rem 密钥: .env 不入库, 新机从 env.example 拷
if not exist .env (
  echo [!] 缺 .env: copy env.example .env 后填密钥
  exit /b 1
)

rem [2/3] 依赖: 有 feishu extra 的分支(develop, gateway 所在)带上它; master 普通 sync
set "EXTRA="
findstr /c:"feishu" pyproject.toml >nul 2>nul && set "EXTRA=--extra feishu"
echo [2/3] uv sync %EXTRA%
uv sync %EXTRA%
if errorlevel 1 exit /b 1

rem 浏览器内核: 浏览器属 develop 增量(master 无 core/browser.py, 跳过); 安装脚本走 Git Bash
if exist core\browser.py (
  echo [browser] 本分支有浏览器模块, 内核安装: Git Bash 里跑 bash install_browser.sh
) else (
  echo [browser] 本分支无浏览器模块, 跳过内核安装
)

rem [3/3] 全局命令: 代码路径钉死本 checkout, 调用时的 cwd 即 agent 工作目录。
rem master 装 zany; develop 装 zany-dev(避免覆盖全局 zany 到 develop 代码)
set "CMD=zany"
for /f "delims=" %%b in ('git branch --show-current') do if /i "%%b"=="develop" set "CMD=zany-dev"
set "BIN=%USERPROFILE%\.local\bin"
if not exist "%BIN%" mkdir "%BIN%"
> "%BIN%\%CMD%.cmd" echo @echo off
>>"%BIN%\%CMD%.cmd" echo rem zany coding agent: cwd 即 agent 工作目录^(AGENTS.md/文件工具/execute 全锚定它^)
>>"%BIN%\%CMD%.cmd" echo set "VIRTUAL_ENV="
>>"%BIN%\%CMD%.cmd" echo uv run --project "%REPO%" python "%REPO%\tui.py" %%*
echo [3/3] 全局命令: %BIN%\%CMD%.cmd

echo.
echo 部署完成。用法(任意目录):
echo   %CMD%                    TUI
echo   %CMD% -p "..."           无头单次
echo   uv run eval/main.py [name]  评测
endlocal
