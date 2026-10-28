# ============================================
# AI Reader V2 本地开发一键启动脚本（Windows）
# 用法：右键 -> 使用 PowerShell 运行，或
#       powershell -ExecutionPolicy Bypass -File start.ps1
# 前置：已执行过  backend: uv sync --all-extras
#                  frontend: npm install
# ============================================

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot

# 0. 端口占用检查（避免重复启动）
$port8000 = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue
$port5173 = Get-NetTCPConnection -LocalPort 5173 -State Listen -ErrorAction SilentlyContinue
if ($port8000 -or $port5173) {
    Write-Host "[警告] 8000 或 5173 端口已被占用，服务可能已在运行。" -ForegroundColor Yellow
    Write-Host "       若想重启，请先关闭已有实例（任务管理器结束 python/node 进程）。" -ForegroundColor Yellow
    Write-Host ""
}

# 1. 启动后端（FastAPI，端口 8000）
$py = Join-Path $root "backend\.venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $py)) {
    Write-Host "[错误] 未找到后端虚拟环境: $py" -ForegroundColor Red
    Write-Host "       请先在 backend 目录执行: uv sync --all-extras" -ForegroundColor Red
    exit 1
}
$backend = Start-Process -FilePath $py -ArgumentList "-m","uvicorn","src.api.main:app","--host","127.0.0.1","--port","8000" -WorkingDirectory (Join-Path $root "backend") -PassThru -WindowStyle Hidden
Write-Host "[后端] 已启动 PID $($backend.Id) -> http://127.0.0.1:8000" -ForegroundColor Green

# 2. 启动前端（Vite，端口 5173）
$npm = (Get-Command npm.cmd -ErrorAction SilentlyContinue).Source
if (-not $npm) { $npm = "npm.cmd" }
$frontend = Start-Process -FilePath $npm -ArgumentList "run","dev" -WorkingDirectory (Join-Path $root "frontend") -PassThru -WindowStyle Hidden
Write-Host "[前端] 已启动 PID $($frontend.Id) -> http://localhost:5173" -ForegroundColor Green

Write-Host ""
Write-Host "============================================" -ForegroundColor Cyan
Write-Host " 浏览器打开:  http://localhost:5173" -ForegroundColor Cyan
Write-Host " 后端文档:    http://127.0.0.1:8000/docs" -ForegroundColor Cyan
Write-Host " 停止服务:    任务管理器结束 PID $($backend.Id) 与 $($frontend.Id)" -ForegroundColor Cyan
Write-Host "============================================" -ForegroundColor Cyan
