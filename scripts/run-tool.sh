#!/usr/bin/env bash
# run-tool.sh — AI PM Agent 项目 Python 工具脚本统一包装器
#
# 用途：统一封装"用 hermes venv 的 Python 跑项目内 scripts/ 下脚本"所需的环境设置：
#   1. 设 PATH 让 hermes venv 的 python 优先（保证依赖可用、与 Pi 运行环境一致）
#   2. 导出 PYTHONPATH=src（项目内模块都在 src/ 下，导入 kernel / kb / cli 等）
#   3. 清空 HTTP_PROXY / HTTPS_PROXY / http_proxy / https_proxy（代理会掐断长响应）
#   4. 切换 cwd 到项目根（scripts 目录的上一级）
#   5. 用 exec 透传剩余参数给 python
#
# 用法：
#   ./scripts/run-tool.sh scripts/kb_query.py --query "用户访谈"
#   ./scripts/run-tool.sh scripts/web_search.py --query "AI 产品经理" --top-k 5
#   bash scripts/run-tool.sh scripts/page_fetch.py --url "https://example.com"
#
# 退出码：跟随被执行脚本的退出码；hermes python 不存在时退出 1。
#
# [C 2026-09-10] T4-1 工具脚本包装器

set -euo pipefail

# 项目根 = 本脚本所在 scripts 目录的上一级
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

# hermes venv 的 python（Git Bash 中 .exe 可省略扩展名）
HERMES_BIN="/c/Users/A/AppData/Local/hermes/hermes-agent/venv/Scripts"
HERMES_PY="$HERMES_BIN/python"

if [ ! -x "$HERMES_PY" ] && [ ! -x "$HERMES_PY.exe" ]; then
    echo "[run-tool.sh] 错误：未找到 hermes venv 的 python：$HERMES_PY" >&2
    echo "[run-tool.sh] 请确认 hermes-agent 已安装，或修改脚本顶部 HERMES_BIN 路径。" >&2
    exit 1
fi

# 1) 让 hermes venv 的 python 优先
export PATH="$HERMES_BIN:$PATH"

# 2) PYTHONPATH：项目 src 目录
#    有两个坑，缺一不可，勿改回去：
#    (a) 路径形式：PROJECT_ROOT 由 pwd 得到，在 Git Bash 里是 MSYS 风格
#        （如 /d/ai-pm-agent），直接拼出 /d/ai-pm-agent/src；而这里 exec 的 python
#        是原生 Windows 程序，不认 MSYS 路径形式 → src 不在搜索路径里 →
#        import kb/kernel 等项目模块全部 ModuleNotFoundError。
#        MSYS 的路径自动转换只对「命令行参数」生效，对「环境变量」不生效。
#        故用 cygpath -m 转成原生 Windows 形式（D:/ai-pm-agent，正斜杠 Python 认），
#        与 cwd 无关，比相对路径更稳。
#    (b) 分隔符：Windows 上 PYTHONPATH 的条目分隔符是「;」不是「:」。
#        若写成 "${PYTHONPATH:+:$PYTHONPATH}"，调用方已有 PYTHONPATH 时（Pi 启动
#        脚本会设）会拼成 "src:src" 这种单条无效路径，同样 ModuleNotFoundError。
#    [C 2026-09-17] S053：修路径形式 + 分隔符，两处都勿改回。
PROJECT_ROOT_WIN="$(cygpath -m "$PROJECT_ROOT")"
export PYTHONPATH="$PROJECT_ROOT_WIN/src${PYTHONPATH:+;$PYTHONPATH}"

# 3) 清空代理环境变量（DeepSeek / 联网抓取都需直连，代理会掐断）
unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy

# 3.5) LiteLLM 用本地模型价格表，不拉远程（远程握手失败会阻塞 400 秒）
#     [MA 2026-09-17] R13 真机发现：ChatLiteLLM 初始化会尝试拉取远程价格表，
#     握手超时阻塞；本地表足够，禁远程后每个脚本启动省 ~400 秒。
export LITELLM_LOCAL_MODEL_COST_MAP=True

# 4) 切换 cwd 到项目根
cd "$PROJECT_ROOT"

# 5) 透传执行
# 用法：run-tool.sh <script.py> [args...]
if [ "$#" -lt 1 ]; then
    echo "[run-tool.sh] 用法：$0 <script.py> [args...]" >&2
    exit 2
fi

exec "$HERMES_PY" "$@"
