# run-pi.ps1 — AI PM Agent 项目启动 Pi 终端 Agent 的包装脚本
# 功能：
#   1. 从项目根 .env 读取 DEEPSEEK_API_KEY 并注入当前进程环境变量
#   2. 清空代理环境变量（DeepSeek 必须直连，代理会掐断 api.deepseek.com 长响应）
#   3. 切换工作目录到项目根
#   4. 透传所有参数启动 pi
# 用法：
#   powershell -ExecutionPolicy Bypass -File scripts\run-pi.ps1              # 交互模式
#   powershell -ExecutionPolicy Bypass -File scripts\run-pi.ps1 -p "你好"    # headless 模式

$ErrorActionPreference = 'Stop'

# 项目根 = 本脚本所在目录（scripts）的上级目录
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$EnvFile = Join-Path $ProjectRoot '.env'

if (-not (Test-Path $EnvFile)) {
    Write-Error "未找到项目 .env 文件：$EnvFile（应包含 DEEPSEEK_API_KEY=... 一行）"
    exit 1
}

# 解析 .env 中的 DEEPSEEK_API_KEY，兼容 KEY=value 与 KEY="value" / KEY='value' 写法
$apiKey = $null
foreach ($line in Get-Content $EnvFile) {
    if ($line -match '^\s*DEEPSEEK_API_KEY\s*=\s*(.*)$') {
        $val = $Matches[1].Trim()
        if ($val.Length -ge 2 -and
            (($val.StartsWith('"') -and $val.EndsWith('"')) -or
             ($val.StartsWith("'") -and $val.EndsWith("'")))) {
            $val = $val.Substring(1, $val.Length - 2)
        }
        $apiKey = $val
        break
    }
}

if ([string]::IsNullOrWhiteSpace($apiKey)) {
    Write-Error ".env 中未找到 DEEPSEEK_API_KEY 配置。请在 $EnvFile 中添加一行：DEEPSEEK_API_KEY=sk-你的密钥"
    exit 1
}

# 注入当前进程环境变量
$env:DEEPSEEK_API_KEY = $apiKey

# 清空代理环境变量（进程级）
foreach ($proxyVar in 'HTTP_PROXY','HTTPS_PROXY','http_proxy','https_proxy') {
    Remove-Item -Path "Env:$proxyVar" -ErrorAction SilentlyContinue
}

# 切换工作目录到项目根（保证 pi 能读到项目根的 AGENTS.md）
Set-Location $ProjectRoot

# 透传所有参数启动 pi
# 说明：固定加 -ne（--no-extensions）——用户目录 ~/.pi/agent/extensions 下存在
# 其他 Pi 发行版遗留的扩展（依赖 @earendil-works/* 模块），在当前 pi 版本下加载
# 失败会导致进程直接退出；本项目只需要内置工具 + AGENTS.md + 项目 .pi/skills，
# 不依赖这些扩展，故禁用扩展发现（不影响技能加载与工具使用）。
& pi -ne @args

# [C 2026-09-09] T2 启动脚本
