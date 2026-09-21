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

# 通用解析 .env：逐行注入所有 KEY=VALUE 到当前进程环境变量
# 兼容 KEY=value 与 KEY="value" / KEY='value'；# 开头的注释行与空行跳过
# 从"只解析 DEEPSEEK_API_KEY"升级为通用注入（新增 ARK_API_KEY 等）
foreach ($line in Get-Content $EnvFile) {
    if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') {
        $envName = $Matches[1]
        $val = $Matches[2].Trim()
        if ($val.Length -ge 2 -and
            (($val.StartsWith('"') -and $val.EndsWith('"')) -or
             ($val.StartsWith("'") -and $val.EndsWith("'")))) {
            $val = $val.Substring(1, $val.Length - 2)
        }
        Set-Item -Path "Env:$envName" -Value $val
    }
}

if ([string]::IsNullOrWhiteSpace($env:DEEPSEEK_API_KEY)) {
    Write-Error ".env 中未找到 DEEPSEEK_API_KEY 配置。请在 $EnvFile 中添加一行：DEEPSEEK_API_KEY=sk-你的密钥"
    exit 1
}

# 清空代理环境变量（进程级）
foreach ($proxyVar in 'HTTP_PROXY','HTTPS_PROXY','http_proxy','https_proxy') {
    Remove-Item -Path "Env:$proxyVar" -ErrorAction SilentlyContinue
}

# 切换工作目录到项目根（保证 pi 能读到项目根的 AGENTS.md）
Set-Location $ProjectRoot

# 组装启动参数
$piArgs = @('-ne')

# 加载演示工具包中"环境已就绪"的技能（T5；工具清单与状态见 references/外置工具路由.md）
# 说明：技能实体不复制进项目 .pi/skills，pi 通过 --skill <目录> 直接加载（工具包不在则跳过，不影响启动）；
# 只登记环境已就绪的技能——未就绪的不加载，避免 Agent 误以为可用；环境配好后在此追加一行即可。
# 技能加载接线；追加 diagram-mermaid
# 追加 ppt-master（技能在仓库嵌套目录 skills\ppt-master 内）
# 工具包随参考库迁入项目内 工具与参考\Agent外置工具包\，路径改为基于 $ProjectRoot 推算
$DemoToolkit = Join-Path $ProjectRoot '工具与参考\Agent外置工具包\演示工具包'
# AI 评测工具包（Promptfoo 执行器外置），薄技能 ai-eval
$AiEvalToolkit = Join-Path $ProjectRoot '工具与参考\Agent外置工具包\AI评测工具包'
$ExtSkillPaths = @(
    (Join-Path $DemoToolkit 'skills\frontend-slides'),
    (Join-Path $DemoToolkit 'skills\lieflat-charts'),
    (Join-Path $DemoToolkit 'skills\diagram-mermaid'),
    (Join-Path $DemoToolkit 'skills\ppt-master\skills\ppt-master'),
    (Join-Path $AiEvalToolkit 'skills\ai-eval')
)
foreach ($skillDir in $ExtSkillPaths) {
    if (Test-Path (Join-Path $skillDir 'SKILL.md')) {
        $piArgs += @('--skill', $skillDir)
    }
}

# ppt-master 独立 venv（Python 3.12，依赖装于此外置包内，与 hermes venv 隔离）。
# 若该 venv 存在，将其 Scripts 前置到 PATH——技能脚本中的 python3/python 命令即命中本 venv
# （Scripts 下有 python.exe 与 python3.cmd 别名）；不影响 run-tool.sh（它用 hermes 绝对路径）。
$PptVenvScripts = Join-Path $DemoToolkit 'tools\ppt-master-venv\Scripts'
if (Test-Path (Join-Path $PptVenvScripts 'python.exe')) {
    $env:PATH = "$PptVenvScripts;$env:PATH"
}

# 透传用户传入的所有参数
# 说明：固定加 -ne（--no-extensions）——用户目录 ~/.pi/agent/extensions 下存在
# 其他 Pi 发行版遗留的扩展（依赖 @earendil-works/* 模块），在当前 pi 版本下加载
# 失败会导致进程直接退出；本项目只需要内置工具 + AGENTS.md + 项目 .pi/skills，
# 不依赖这些扩展，故禁用扩展发现（不影响技能加载与工具使用）。
$piArgs += $args
& pi @piArgs

# 启动脚本
