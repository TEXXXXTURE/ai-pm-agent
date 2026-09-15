# [C 2026-09-12 by codebuddy-ds41flash] 构建期跑评测节点（eval_run，第 8 段）
"""构建期跑评测：把 S035 产出的评测体系 YAML 草案变成真实执行 + 代码硬判结论。

图位置（第 8 段，仅 AI 核心需求经过；插在「确认工单」确认分支之后、「写发布计划」之前）：
    issue_confirm --(确认 且 ai_core=True)--> eval_run --(passed)--> launch_plan
                                                 eval_run --(未达标/工具错误)--> eval_run（自环重跑）
普通需求（ai_core=False/None）确认分支直达 launch_plan（route 返回 "artifact_persist" 语义值
由 graph 映射到 launch_plan），行为与现状逐字一致，完全不经本节点。

节点内部四步（不调模型）：
    ① finalize_eval_config 备可执行配置（prompts 换 file://、provider 归一、showThinking 关）；
    ② subprocess 调 Promptfoo（退出码 0/100 跑通、1 工具错误）；
    ③ parse_promptfoo_results 解析 results.json；
    ④ judge_eval_report 按双及格线代码硬判（模型不决定走向）。

三种暂停（都不自动空转，靠人修复后恢复重跑）：
- await_prompt：评测目录缺 system_prompt.txt，放入后恢复；
- tool_error  ：Promptfoo 退出码 1（工具/配置/网络错误），修好后恢复；
- eval_failed ：工具跑通但未达及格线，工程师线下改 prompt/题/模型方案后恢复，**不设自动放行**。
计数：每次实际执行（跑到出 results.json）eval_run_count +1；await_prompt 阶段不计。

公共件（第 6 段 bake_off 复用）：``run_promptfoo_eval``（入口解析/env/subprocess/results 读取）
与 ``collect_critical_exams`` / ``collect_critical_descriptions`` / ``critical_pass_stats``
（关键题判定），均定义在本模块，行为与抽取前逐字一致。
"""
from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime
from pathlib import Path

import yaml
from langgraph.types import interrupt

from kernel.exceptions import NodeExecutionError
from nodes.eval_design import PROMPTFOO_JUDGE_PROVIDER

# Promptfoo 退出码（见 AI 评测工具包 skills/ai-eval/SKILL.md） [C 2026-09-12 by codebuddy-ds41flash]
EXIT_OK = 0            # 全部断言通过
EXIT_CONTENT_FAIL = 100  # 工具跑通，至少一条断言失败（内容问题，非工具错误）
EXIT_TOOL_ERROR = 1    # 工具/配置/网络错误

# 单次评测执行超时（秒）
PROMPTFOO_TIMEOUT = 600

# 代理环境变量：调模型前必须剔除（代理会掐断 DeepSeek 长响应）
_PROXY_ENV_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")

# stderr/stdout 摘录长度上限（中断载荷 reason 用）
_REASON_TAIL_LENGTH = 500

# provider 归一后的固定 config（S031 样例验证过的形态；showThinking:false 避免思考段污染断言）
# max_tokens=32768（2026-09-15 真机复测上调，原 2048）：deepseek-v4-flash 带隐藏思考，
# 2048 会被 reasoning 全部占满、可见正文为空（finishReason=length、output=""），空正文再被
# 判为「不通过」——把配置问题伪装成模型质量结论；32768 与 config.yaml 单次输出上限口径一致。
_NORMALIZED_PROVIDER_CONFIG: dict = {
    "temperature": 0,
    "max_tokens": 32768,
    "showThinking": False,
}

# prompts 段替换为同目录 txt 引用。
# 用相对路径 ./system_prompt.txt 而非 file://：后者在含中文/空格的路径下 URI 解析会失败，
# 相对路径由 Promptfoo 按配置文件所在目录解析，跨平台稳定。
_SYSTEM_PROMPT_REF = "./system_prompt.txt"

# 需要模型阅卷的断言类型（缺 provider 时 Promptfoo 回退默认 OpenAI/Codex 通道）
_JUDGE_ASSERTION_TYPES = ("llm-rubric", "llm-output-rubric", "model-graded-closedqa")


def ensure_judge_provider(document: dict, judge_provider: str = PROMPTFOO_JUDGE_PROVIDER) -> dict:
    """纯函数：给每条缺 provider 的模型阅卷断言补上阅卷模型（S039 真机修复）。

    新草案在 eval_design 渲染时已带 provider；本函数兜底两类场景：
    ①修复前渲染、已冻结在 state 里的旧草案（正在 eval_failed 中断点的会话）；
    ②bake_off 从同一草案派生的单模型配置。
    已有 provider 的断言原样保留，tests/assert 结构异常时安全跳过。
    """
    tests = document.get("tests")
    if not isinstance(tests, list):
        return document
    for test in tests:
        if not isinstance(test, dict):
            continue
        assertions = test.get("assert")
        if not isinstance(assertions, list):
            continue
        for assertion in assertions:
            if not isinstance(assertion, dict):
                continue
            if assertion.get("type") in _JUDGE_ASSERTION_TYPES and not assertion.get("provider"):
                assertion["provider"] = judge_provider
    return document


def finalize_eval_config(eval_yaml_draft: str) -> str:
    """纯函数：把 S035 渲染的 Promptfoo YAML 草案归一为可执行配置文本。

    处理：
    - `prompts` 从占位 ``{{prd_core_task_prompt}}`` 替换为 ``["file://system_prompt.txt"]``；
    - `providers` 统一归一为 S031 样例验证过的字典形态
      ``{"id": <合法 provider id>, "config": {temperature:0, max_tokens:2048, showThinking:false}}``；
      原 provider 是合法 id 字符串时包成上述字典；providers 为空时补默认 DeepSeek id；
    - `tests` 保留；其中缺 provider 的 llm-rubric 类断言统一补阅卷模型
      （``PROMPTFOO_JUDGE_PROVIDER``，S039 真机修复）。

    Returns:
        归一后的 YAML 文本（``yaml.safe_dump``，allow_unicode=True，sort_keys=False）；
        **输入解析失败或顶层非 dict 时返回空串**（调用方据此判工具错误）。
    """
    try:
        document = yaml.safe_load(eval_yaml_draft)
    except Exception:
        return ""
    if not isinstance(document, dict):
        return ""

    document["prompts"] = [_SYSTEM_PROMPT_REF]

    raw_providers = document.get("providers") or []
    if not isinstance(raw_providers, list):
        raw_providers = [raw_providers]
    normalized: list[dict] = []
    for provider in raw_providers:
        if isinstance(provider, dict):
            provider_id = str(provider.get("id") or "")
        else:
            provider_id = str(provider or "")
        if not provider_id:
            continue
        normalized.append(
            {"id": provider_id, "config": dict(_NORMALIZED_PROVIDER_CONFIG)}
        )
    if not normalized:
        normalized = [
            {
                "id": "deepseek:deepseek-v4-flash",
                "config": dict(_NORMALIZED_PROVIDER_CONFIG),
            }
        ]
    document["providers"] = normalized

    # 旧草案里的 llm-rubric 断言可能缺阅卷模型，统一补齐（S039 真机修复）
    ensure_judge_provider(document)

    return yaml.safe_dump(document, allow_unicode=True, sort_keys=False)
    # [C 2026-09-12 by codebuddy-ds41flash] 评测配置归一纯函数


def parse_promptfoo_results(results: dict) -> dict:
    """纯函数：解析 Promptfoo 0.123.0 的 results.json 顶层 dict（缺字段安全降级）。

    Args:
        results: results.json 顶层 dict（含 ``results`` 数组与 ``stats``）。

    Returns:
        ``{successes, failures, errors, total, per_exam, token_usage, cost, duration_ms}``：
        - successes/failures/errors 从 ``stats`` 取（缺省 0），**errors 计入 failures**
          （工具级错误视为该题失败）；total = successes + failures；
        - per_exam：逐题 ``{test_idx, description, success, score, latency_ms,
          output_snippet, grading_pass}``（output_snippet 截取前 200 字符）；
        - token_usage：从 ``stats.tokenUsage`` 取 prompt/completion/total（缺省 0）；
        - cost：``stats.totalCost`` 或逐题 cost 求和（缺省 0.0）；
        - duration_ms：``stats.durationMs``（缺省 0）。
    """
    data = results if isinstance(results, dict) else {}
    # Promptfoo 0.123.0 的 results.json：顶层 ``results`` 是 dict（含 version/timestamp/
    # prompts/results/stats），真正的逐题列表在 ``results.results``，stats 在 ``results.stats``；
    # 早期版本顶层 ``results`` 直接是 list，stats 在顶层。两种都兼容。
    inner = data.get("results")
    if isinstance(inner, dict):
        stats = inner.get("stats") if isinstance(inner.get("stats"), dict) else {}
        raw_results = inner.get("results") if isinstance(inner.get("results"), list) else []
    else:
        stats = data.get("stats") if isinstance(data.get("stats"), dict) else {}
        raw_results = inner if isinstance(inner, list) else []

    successes = _to_int(stats.get("successes"))
    errors = _to_int(stats.get("errors"))
    # errors 计入 failures：工具级错误视为该题失败，保证 successes + failures == total
    failures = _to_int(stats.get("failures")) + errors

    per_exam: list[dict] = []
    for item in raw_results:
        if not isinstance(item, dict):
            continue
        response = item.get("response") if isinstance(item.get("response"), dict) else {}
        output = response.get("output")
        output_snippet = "" if output is None else str(output)[:200]
        grading = (
            item.get("gradingResult")
            if isinstance(item.get("gradingResult"), dict)
            else {}
        )
        # Promptfoo 0.123.0：题目描述在 testCase.description，不在 item 顶层。
        test_case = item.get("testCase") if isinstance(item.get("testCase"), dict) else {}
        description = item.get("description") or test_case.get("description") or ""
        per_exam.append(
            {
                "test_idx": item.get("testIdx"),
                "description": str(description),
                "success": bool(item.get("success", False)),
                "score": item.get("score"),
                "latency_ms": item.get("latencyMs"),
                "output_snippet": output_snippet,
                "grading_pass": grading.get("pass"),
            }
        )

    token_raw = stats.get("tokenUsage") if isinstance(stats.get("tokenUsage"), dict) else {}
    token_usage = {
        "prompt": _to_int(token_raw.get("prompt")),
        "completion": _to_int(token_raw.get("completion")),
        "total": _to_int(token_raw.get("total")),
    }

    cost = stats.get("totalCost")
    if cost is None:
        cost = 0.0
        for item in raw_results:
            if isinstance(item, dict) and item.get("cost") is not None:
                cost += _to_float(item.get("cost"))
    else:
        cost = _to_float(cost)

    return {
        "successes": successes,
        "failures": failures,
        "errors": errors,
        "total": successes + failures,
        "per_exam": per_exam,
        "token_usage": token_usage,
        "cost": cost,
        "duration_ms": stats.get("durationMs") or 0,
    }
    # [C 2026-09-12 by codebuddy-ds41flash] results.json 解析纯函数（缺字段安全降级）


def _to_int(value: object) -> int:
    """安全转 int（None/非法值 -> 0）。"""
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _to_float(value: object) -> float:
    """安全转 float（None/非法值 -> 0.0）。"""
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def _match_exam_result(exam: dict, per_exam: list[dict]) -> dict | None:
    """用 description 或 id 在逐题结果里匹配一道考题（匹配不到返回 None）。"""
    exam_id = str(exam.get("id") or "")
    description = str(exam.get("description") or "")
    for item in per_exam:
        item_desc = str(item.get("description") or "")
        if exam_id and exam_id in item_desc:
            return item
        if description and description in item_desc:
            return item
    return None


def collect_critical_exams(exam_sets: list) -> list[dict]:
    """公共：收集关键题——对抗层全部题 + 典型层且 critical=True 的题。

    关键题口径的唯一定义处，judge_eval_report 与 bake_off 汇总共用。
    """
    critical: list[dict] = []
    for exam_set in exam_sets or []:
        if not isinstance(exam_set, dict):
            continue
        layer = str(exam_set.get("layer") or "")
        exams = exam_set.get("exams") or []
        if not isinstance(exams, list):
            continue
        for exam in exams:
            if not isinstance(exam, dict):
                continue
            is_critical = layer == "adversarial" or (
                layer == "typical" and bool(exam.get("critical", False))
            )
            if is_critical:
                critical.append({**exam, "_layer": layer})
    return critical
    # [C 2026-09-13 by codebuddy-ds41flash] 由 _collect_critical_exams 提升为公共件，供 bake_off 复用


def collect_critical_descriptions(exam_sets: list) -> list[str]:
    """公共：收集关键题的标签列表（description 优先，其次 id）。

    与 collect_critical_exams 顺序一一对应，供未过关键题展示与 bake_off 汇总共用。
    """
    return [
        str(exam.get("description") or exam.get("id") or "").strip()
        for exam in collect_critical_exams(exam_sets)
    ]
    # [C 2026-09-13 by codebuddy-ds41flash] 关键题标签收集公共件


def critical_pass_stats(exam_sets: list, per_exam: list) -> dict:
    """公共：按 eval_run 口径算关键题通过情况（judge_eval_report 与 bake_off 共用）。

    关键题集合来自 collect_critical_exams；用 description 或 id 与 per_exam 匹配，
    **匹配不到的关键题视为失败**。

    Returns:
        ``{"passed": int, "total": int, "rate": float, "failed_descriptions": list[str]}``；
        关键题总数为 0 时 ``rate=1.0``（不设门槛）。
    """
    exams = collect_critical_exams(exam_sets if isinstance(exam_sets, list) else [])
    labels = collect_critical_descriptions(
        exam_sets if isinstance(exam_sets, list) else []
    )
    per = per_exam if isinstance(per_exam, list) else []
    passed = 0
    failed_descriptions: list[str] = []
    for exam, label in zip(exams, labels):
        matched = _match_exam_result(exam, per)
        if matched is not None and bool(matched.get("success", False)):
            passed += 1
        else:
            failed_descriptions.append(label)
    total = len(exams)
    return {
        "passed": passed,
        "total": total,
        "rate": (passed / total) if total > 0 else 1.0,
        "failed_descriptions": failed_descriptions,
    }
    # [C 2026-09-13 by codebuddy-ds41flash] 关键题通过统计公共件（eval_run/bake_off 共用）


def judge_eval_report(parsed: dict, pass_lines: dict, exam_sets: list) -> dict:
    """纯函数：按双及格线对评测结果做**代码硬判**（模型不参与）。

    - 关键题集合：遍历 exam_sets，``layer=="adversarial"`` 的全部题
      + ``layer=="typical"`` 且 ``exam.critical==True`` 的题；
      用 description 或 id 与 parsed.per_exam 匹配，**匹配不到的关键题视为失败**。
    - ``overall_rate = successes / total``（total=0 时取 0）；
    - ``critical_rate = 关键题中 success=True 的数量 / 关键题总数``
      （关键题为 0 时取 1.0，不设门槛）；
    - 阈值从 pass_lines 取（缺省 0.0）；
    - 达标 = ``overall_rate >= overall_pass_rate AND critical_rate >= critical_pass_rate``。

    Returns:
        ``{passed, overall_rate, critical_rate, overall_threshold, critical_threshold,
        failed_critical_descriptions, gaps}``。
    """
    data = parsed if isinstance(parsed, dict) else {}
    thresholds = pass_lines if isinstance(pass_lines, dict) else {}
    per_exam = data.get("per_exam") if isinstance(data.get("per_exam"), list) else []

    successes = _to_int(data.get("successes"))
    total = _to_int(data.get("total"))
    overall_rate = (successes / total) if total > 0 else 0.0

    # [C 2026-09-13 by codebuddy-ds41flash] 关键题通过统计抽公共件（eval_run/bake_off 共用），
    # 口径与原内联循环逐字一致：匹配不到的关键题视为失败。
    critical_stats = critical_pass_stats(
        exam_sets if isinstance(exam_sets, list) else [], per_exam
    )
    failed_critical_descriptions = critical_stats["failed_descriptions"]
    critical_rate = critical_stats["rate"]

    overall_threshold = _to_float(thresholds.get("overall_pass_rate"))
    critical_threshold = _to_float(thresholds.get("critical_pass_rate"))

    passed = overall_rate >= overall_threshold and critical_rate >= critical_threshold

    return {
        "passed": passed,
        "overall_rate": overall_rate,
        "critical_rate": critical_rate,
        "overall_threshold": overall_threshold,
        "critical_threshold": critical_threshold,
        "failed_critical_descriptions": failed_critical_descriptions,
        "gaps": {
            "overall": f"{overall_rate:.1%} vs {overall_threshold:.1%}",
            "critical": f"{critical_rate:.1%} vs {critical_threshold:.1%}",
        },
    }
    # [C 2026-09-12 by codebuddy-ds41flash] 评测达标硬判纯函数（双及格线，模型不参与）


def route_after_eval_run(state: dict) -> str:
    """条件边路由：按 eval_report.passed 两态。

    - ``eval_report.passed == True`` -> ``launch_plan``；
    - 其余（未达标/工具错误/待 prompt，这些状态都在节点内部 interrupt，graph 不会拿到
      未达标返回值；此处保守兜底）-> ``eval_run``（自环重跑）。
    """
    report = state.get("eval_report") or {}
    if isinstance(report, dict) and report.get("passed") is True:
        return "launch_plan"
    return "eval_run"
    # [C 2026-09-12 by codebuddy-ds41flash] 评测达标两态条件边路由纯函数


def _stderr_tail(proc: object) -> str:
    """取 subprocess 结果的 stderr 尾部（stderr 为空时退回 stdout），上限 500 字符。"""
    stderr = str(getattr(proc, "stderr", "") or "")
    stdout = str(getattr(proc, "stdout", "") or "")
    text = stderr.strip() or stdout.strip()
    return text[-_REASON_TAIL_LENGTH:]


def _resolve_promptfoo_entry(promptfoo_dir: Path) -> Path:
    """解析 Promptfoo 可执行入口 JS 的绝对路径（版本无关，供 ``node <entry>`` 调用）。

    Promptfoo 不同版本入口不同（0.123.0 的 package.json ``bin`` 指向 ``dist/src/entrypoint.js``，
    而早期文档写 ``bin/promptfoo``）。这里按优先级探测，避免写死版本相关路径：
    1. 读 ``node_modules/promptfoo/package.json`` 的 ``bin`` 字段推导（最稳）；
    2. 任务书指定的 ``node_modules/promptfoo/bin/promptfoo``；
    3. 兜底 ``node_modules/promptfoo/dist/src/entrypoint.js``。
    """
    package_dir = promptfoo_dir / "node_modules" / "promptfoo"
    package_json = package_dir / "package.json"
    if package_json.exists():
        try:
            data = json.loads(package_json.read_text(encoding="utf-8"))
            bin_field = data.get("bin")
            if isinstance(bin_field, dict):
                rel = bin_field.get("promptfoo") or next(iter(bin_field.values()), None)
            elif isinstance(bin_field, str):
                rel = bin_field
            else:
                rel = None
            if rel:
                candidate = (package_dir / rel).resolve()
                if candidate.exists():
                    return candidate
        except Exception:
            pass
    legacy = package_dir / "bin" / "promptfoo"
    if legacy.exists():
        return legacy
    return package_dir / "dist" / "src" / "entrypoint.js"
    # [C 2026-09-12 by codebuddy-ds41flash] Promptfoo 入口版本无关解析（task 书 bin/promptfoo 为候选之一）


def run_promptfoo_eval(
    promptfoo_dir: object,
    config_path: object,
    eval_dir: object,
    timeout: int = PROMPTFOO_TIMEOUT,
) -> tuple[int, str, dict | None]:
    """公共执行件：解析入口 + 剔除代理 env + subprocess 调 Promptfoo + 读 results.json。

    eval_run 与 bake_off 共用；封装原 eval_run 内联执行循环的机械部分，
    对照 eval_run 原逻辑逐字一致（入口解析 / env 处理 / 命令拼装 / 超时与启动失败处置 /
    退出码与 results.json 读取）。

    Args:
        promptfoo_dir: Promptfoo 安装目录（含 node_modules/promptfoo）。
        config_path: 本次评测的 Promptfoo 配置路径（绝对化后传给 -c）。
        eval_dir: 执行目录（cwd），results.json 也从此目录读取。
        timeout: 单次执行超时秒数。

    Returns:
        ``(returncode, stderr_tail, results_json)``：
        - returncode：Promptfoo 退出码；超时/无法启动统一为 ``EXIT_TOOL_ERROR``；
        - stderr_tail：stderr（空则 stdout）尾部，上限 500 字符；超时/启动失败为说明文本；
        - results_json：退出码 0/100 且 ``<eval_dir>/results.json`` 存在且为合法 JSON 时返回
          解析后 dict，否则 None（调用方据文件是否存在与内容给出与 eval_run 一致的提示）。
    """
    promptfoo_bin = _resolve_promptfoo_entry(Path(promptfoo_dir))
    eval_dir_path = Path(eval_dir)
    raw_results_path = eval_dir_path / "results.json"
    env = {k: v for k, v in os.environ.items() if k not in _PROXY_ENV_KEYS}
    # 用绝对路径：cwd 会切到 eval_dir，相对路径会解析错位。
    command = [
        "node",
        str(promptfoo_bin),
        "eval",
        "-c",
        str(Path(config_path).resolve()),
        "-o",
        str(raw_results_path.resolve()),
        "--no-cache",
    ]

    try:
        proc = subprocess.run(
            command,
            cwd=str(eval_dir_path),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        returncode = proc.returncode
        stderr_tail = _stderr_tail(proc)
    except subprocess.TimeoutExpired as exc:
        return EXIT_TOOL_ERROR, f"评测执行超时（>{timeout}s）：{exc}", None
    except OSError as exc:
        return EXIT_TOOL_ERROR, f"无法启动 Promptfoo：{exc}", None

    # 非 0/100：工具错误（调用方中断）；返回码原样带回，stderr_tail 供展示
    if returncode not in (EXIT_OK, EXIT_CONTENT_FAIL):
        return returncode, stderr_tail, None

    # 0/100：读 results.json；缺失/非法也判工具错误（调用方据文件状态给提示）
    if not raw_results_path.exists():
        return returncode, stderr_tail, None
    try:
        results_json = json.loads(raw_results_path.read_text(encoding="utf-8"))
    except (ValueError, TypeError):
        return returncode, stderr_tail, None
    return returncode, stderr_tail, results_json
    # [C 2026-09-13 by codebuddy-ds41flash] Promptfoo 执行公共件（eval_run/bake_off 共用）


def make_eval_run(deps):
    """构建期跑评测节点工厂：返回签名 (state: dict) -> dict 的节点函数（不调模型）。

    仅 AI 核心需求（ai_core=True）执行；非真直接返回 ``{}``（普通轨到了也直通，不阻断）。
    依赖 ``deps.eval_tool["promptfoo_dir"]`` 定位 Promptfoo 执行器；缺配置抛 NodeExecutionError。
    """

    def eval_run(state: dict) -> dict:
        # 1. 非 AI 核心需求直通（普通轨不应到本节点，到了也放行）
        if not state.get("ai_core"):
            return {}

        # 2. 上游产物缺失：不静默，抛错
        eval_yaml_draft = state.get("eval_yaml_draft") or ""
        eval_archive = state.get("eval_archive") or {}
        eval_system = state.get("eval_system") or {}
        if not eval_yaml_draft or not eval_archive or not eval_system:
            raise NodeExecutionError(
                "eval_run",
                "缺少 eval_yaml_draft / eval_archive / eval_system，无法执行评测",
            )

        eval_tool = deps.eval_tool
        if not isinstance(eval_tool, dict) or not eval_tool.get("promptfoo_dir"):
            raise NodeExecutionError(
                "eval_run",
                "未配置 eval_tool.promptfoo_dir，无法定位 Promptfoo 执行器",
            )

        name = state.get("requirement_name") or "未命名需求"
        generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        run_count = _to_int(state.get("eval_run_count"))

        # 3. 备可执行配置并落盘（解析失败 -> 判工具错误，中断等修复）
        config_text = finalize_eval_config(eval_yaml_draft)
        if not config_text:
            interrupt(
                {
                    "node": "eval_run",
                    "status": "tool_error",
                    "reason": "评测配置归一失败：eval_yaml_draft 无法解析为合法 YAML",
                    "requirement_name": name,
                }
            )
        config_path = deps.artifacts.save(config_text, name, "eval_config", ext=".yaml")
        eval_dir = Path(config_path).parent

        # 4. 被测 system_prompt.txt 检查（await_prompt）：缺文件则中断等放入，恢复后重查
        while True:
            prompt_path = eval_dir / "system_prompt.txt"
            if prompt_path.exists():
                break
            interrupt(
                {
                    "node": "eval_run",
                    "status": "await_prompt",
                    "reason": "评测目录缺少被测 system_prompt.txt，请放入后恢复",
                    "requirement_name": name,
                    "prompt_path": str(prompt_path.resolve()),
                }
            )

        raw_results_path = eval_dir / "results.json"

        # 5~9. 执行循环：工具错误中断重跑；跑通后判定，未达标中断重跑（不自动放行）
        # [C 2026-09-13 by codebuddy-ds41flash] 入口解析/env/subprocess/results 读取抽到
        # run_promptfoo_eval（与 bake_off 共用）；退出码与 results.json 的提示文案逐字不变。
        while True:
            returncode, stderr_tail, results_json = run_promptfoo_eval(
                eval_tool["promptfoo_dir"],
                config_path,
                eval_dir,
                timeout=PROMPTFOO_TIMEOUT,
            )

            # 6. 退出码处置：1（及其他非预期码）判工具错误 -> 中断，恢复后回到本步重跑
            if returncode not in (EXIT_OK, EXIT_CONTENT_FAIL):
                interrupt(
                    {
                        "node": "eval_run",
                        "status": "tool_error",
                        "reason": stderr_tail or f"Promptfoo 退出码 {returncode}",
                        "requirement_name": name,
                    }
                )
                continue

            # 0/100：工具跑通；results.json 缺失/非法也判工具错误（提示与重构前一致）
            if results_json is None:
                if not raw_results_path.exists():
                    reason = "Promptfoo 退出码正常但未生成 results.json"
                else:
                    reason = "results.json 无法解析为合法 JSON"
                interrupt(
                    {
                        "node": "eval_run",
                        "status": "tool_error",
                        "reason": reason,
                        "requirement_name": name,
                    }
                )
                continue
            raw_text = raw_results_path.read_text(encoding="utf-8")

            # 7. 计数 +1（本次实际执行跑到出 results.json）、解析 + 硬判 + 落盘报告
            run_count += 1
            parsed = parse_promptfoo_results(results_json)
            judge = judge_eval_report(
                parsed,
                eval_archive.get("pass_lines") or {},
                eval_system.get("exam_sets") or [],
            )

            report_md = deps.artifacts.render(
                "eval_report.md.j2",
                {
                    "requirement_name": name,
                    "generated_at": generated_at,
                    "parsed": parsed,
                    "judge": judge,
                },
            ).strip()
            report_path = deps.artifacts.save(report_md, name, "eval_report", ext=".md")
            results_path = deps.artifacts.save(raw_text, name, "eval_results", ext=".json")

            eval_report = {
                **judge,
                "successes": parsed["successes"],
                "failures": parsed["failures"],
                "errors": parsed["errors"],
                "total": parsed["total"],
                "per_exam": parsed["per_exam"],
                "token_usage": parsed["token_usage"],
                "cost": parsed["cost"],
                "duration_ms": parsed["duration_ms"],
                "run_count": run_count,
            }
            eval_artifacts = {
                "config_path": str(config_path),
                "results_path": str(results_path),
                "report_path": str(report_path),
            }

            # 9. 未达标：报告已落盘，中断请工程师线下修复后重跑（不自动放行）
            if not judge["passed"]:
                interrupt(
                    {
                        "node": "eval_run",
                        "status": "eval_failed",
                        "reason": "评测未达及格线",
                        "requirement_name": name,
                        "report": judge,
                        "eval_report": eval_report,
                    }
                )
                continue

            # 8. 达标：放行去 launch_plan
            return {
                "eval_report": eval_report,
                "eval_run_count": run_count,
                "eval_artifacts": eval_artifacts,
            }

    return eval_run
    # [C 2026-09-12 by codebuddy-ds41flash] eval_run 节点主体完成（四步 + 三种暂停，不自动空转）


# [C 2026-09-12 by codebuddy-ds41flash] nodes/eval_run.py 新增完成
