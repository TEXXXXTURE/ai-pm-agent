# [C 2026-09-13 by codebuddy-ds41flash] 对比选型模型节点（bake_off，第 6 段）
"""对比选型模型：用第 5 段定稿的同一批考题，经 Promptfoo 对候选模型逐个横跑，
产出质量（通过率）/ 成本 / 延迟（P50、P95）三维对比，**代码硬判推荐模型**，写入选型档案。

图位置（第 6 段，仅 AI 核心需求经过；插在「确认评测体系」确认分支之后、「拆研发工单」之前）：
    eval_confirm --(pass)--> bake_off --(ran/skipped)--> issue_splitting
                             bake_off --(工具错误恢复重跑)--> bake_off（自环）
普通轨（ai_core=False/None）在 prd_review 后直达 issue_splitting，根本不到本节点；
节点内保留 ``if not state.get("ai_core"): return {}`` 防御直通。

节点内部流程（不调流水线模型，只 subprocess 调 Promptfoo）：
    ① 校验上游产物与 bake_off 配置；② 首次 interrupt（await_decision）人工选择跑/跳过；
    ③ run：逐候选生成单模型 YAML 落盘 -> 复用 eval_run 公共执行件跑 Promptfoo ->
       解析 results.json；④ aggregate_candidate_stats 汇总三维；⑤ judge_bakeoff 硬判推荐；
    ⑥ 渲染报告 + 写 model_selection / eval_archive / bakeoff_artifacts。

暂停（都不自动空转，靠人修复后恢复重跑）：
- await_decision：首次确认跑/跳过（其他文本不猜、继续中断）；
- await_prompt  ：评测目录缺 system_prompt.txt（口径同 eval_run）；
- tool_error     ：Promptfoo 退出码非 0/100、配置生成失败、results.json 缺失/非法。

结论一律代码硬判（judge_bakeoff），模型不参与推荐结论。
"""
from __future__ import annotations

import math
from datetime import datetime
from pathlib import Path

import yaml
from langgraph.types import interrupt

from kernel.artifact import sanitize_name
from kernel.exceptions import NodeExecutionError
from nodes.eval_run import (
    EXIT_CONTENT_FAIL,
    EXIT_OK,
    PROMPTFOO_TIMEOUT,
    critical_pass_stats,
    parse_promptfoo_results,
    run_promptfoo_eval,
)

# 跳过横跑时的默认推荐模型（项目自有 DeepSeek 主模型）
_DEFAULT_RECOMMENDED_PROVIDER_ID = "deepseek:deepseek-chat"

# prompts 段替换为同目录 txt 引用（口径同 eval_run：相对路径由 Promptfoo 按配置目录解析）
_SYSTEM_PROMPT_REF = "./system_prompt.txt"

# provider config 按 kind 区分：chat 类用 eval_run 现形；reasoner 类只带 max_tokens
# （reasoner 可能不认 showThinking/temperature 参数，见任务书第四节）
_CHAT_PROVIDER_CONFIG: dict = {
    "temperature": 0,
    "max_tokens": 500,
    "showThinking": False,
}
_REASONER_PROVIDER_CONFIG: dict = {"max_tokens": 500}

# 摘要中要落 state 的候选字段（顺序即落盘顺序）
_SUMMARY_FIELDS: tuple[str, ...] = (
    "provider_id",
    "label",
    "overall_rate",
    "critical_rate",
    "successes",
    "failures",
    "errors",
    "cost",
    "token_total",
    "latency_p50_ms",
    "latency_p95_ms",
)

# ── classify_bakeoff_answer 词表 ──
# 确认词（跑横跑）；跳过词（用默认/不跑）；否定前缀（否定"跳过"=要跑，否定"跑"=跳过）
_BAKEOFF_RUN_WORDS: tuple[str, ...] = (
    "跑",
    "开始",
    "确认",
    "横跑",
    "执行",
    "运行",
    "对比",
    "开跑",
    "run",
    "yes",
    "ok",
    "go",
    "start",
)
_BAKEOFF_SKIP_WORDS: tuple[str, ...] = (
    "跳过",
    "不用",
    "先用默认",
    "用默认",
    "使用默认",
    "直接用",
    "就用",
    "默认",
    "deepseek",
    "skip",
    "no",
    "use default",
)
_BAKEOFF_NEGATION_PREFIXES: tuple[str, ...] = (
    "不",
    "别",
    "勿",
    "甭",
    "无需",
    "不需要",
    "没必要",
    "不要",
    "不用",
)


def _hits(text: str, words: tuple[str, ...]) -> list[tuple[int, str]]:
    """返回 words 在 text 中首次出现的位置列表（(下标, 词)），不出现不返回。"""
    found: list[tuple[int, str]] = []
    for word in words:
        idx = text.find(word)
        if idx != -1:
            found.append((idx, word))
    return found


def _is_negated(text: str, idx: int) -> bool:
    """判断 text[idx] 之前 1~4 个字符是否为否定前缀（如"不跳过"的"不"）。"""
    for length in (4, 3, 2, 1):
        if idx - length >= 0 and text[idx - length: idx] in _BAKEOFF_NEGATION_PREFIXES:
            return True
    return False


def classify_bakeoff_answer(text: object) -> str:
    """纯函数：把模型横跑确认门用户答复归一化三分类为 run / skip / other。

    判定顺序（顺序不可换）：
    1. strip + 英文小写化；
    2. **否定式优先**：否定"跳过"（如"不跳过""别跳过""不用跳过"）= 要跑 → ``run``；
       否定"跑"（如"不跑"）= 跳过 → ``skip``；
    3. 命中确认词（跑/开始/确认/横跑/run…）→ ``run``；
    4. 命中跳过词（跳过/不用/先用默认/直接用deepseek…）→ ``skip``；
    5. 其余（含空串、None、自由讨论文本）→ ``other``（调用方据此继续中断，不猜不空转）。

    Returns:
        ``run`` / ``skip`` / ``other``
    """
    stripped = str(text if text is not None else "").strip()
    lowered = stripped.lower()
    run_hits = _hits(lowered, _BAKEOFF_RUN_WORDS)
    skip_hits = _hits(lowered, _BAKEOFF_SKIP_WORDS)
    negated_run = [hit for hit in run_hits if _is_negated(lowered, hit[0])]
    negated_skip = [hit for hit in skip_hits if _is_negated(lowered, hit[0])]
    positive_run = [hit for hit in run_hits if hit not in negated_run]
    positive_skip = [hit for hit in skip_hits if hit not in negated_skip]

    # 否定式：否定"跳过"= 要跑；否定"跑"= 跳过
    if negated_skip and not negated_run:
        return "run"
    if negated_run and not negated_skip:
        return "skip"
    if positive_run:
        return "run"
    if positive_skip:
        return "skip"
    return "other"
    # [C 2026-09-13 by codebuddy-ds41flash] 模型横跑确认门答复三分类纯函数（否定式防误判）


def build_provider_config(
    eval_yaml_draft: str, provider_id: str, provider_kind: object
) -> str:
    """纯函数：基于 eval_yaml_draft 生成"只留当前一个 provider"的可执行 Promptfoo YAML。

    - ``prompts`` 替换为 ``["./system_prompt.txt"]``（口径同 eval_run）；
    - ``tests`` 原样保留；
    - ``providers`` 只留当前一条：``{"id": <provider_id>, "config": ...}``；
      kind=="reasoner" 用 ``{"max_tokens": 500}``，其余（chat 等）用
      ``{"temperature": 0, "max_tokens": 500, "showThinking": false}``。

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
    if str(provider_kind or "") == "reasoner":
        config = dict(_REASONER_PROVIDER_CONFIG)
    else:
        config = dict(_CHAT_PROVIDER_CONFIG)
    document["providers"] = [{"id": str(provider_id), "config": config}]
    return yaml.safe_dump(document, allow_unicode=True, sort_keys=False)
    # [C 2026-09-13 by codebuddy-ds41flash] 单模型 Promptfoo YAML 生成纯函数（chat/reasoner 分档）


def _percentile(values: list[float], quantile: float) -> float:
    """最近秩法算百分位：排序后取 ``ceil(q*n)-1``（越界夹紧）。

    样本 ≤1 时直接返回该样本（无样本返回 0），故 P50/P95 在单样本下两值相等。
    """
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    idx = math.ceil(quantile * len(ordered)) - 1
    idx = max(0, min(idx, len(ordered) - 1))
    return ordered[idx]


def _safe_int(value: object) -> int:
    """安全转 int（None/非法值 -> 0）。"""
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _safe_float(value: object) -> float:
    """安全转 float（None/非法值 -> 0.0）。"""
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def aggregate_candidate_stats(parsed: dict, exam_sets: list | None = None) -> dict:
    """纯函数：汇总单个候选模型的三维指标（质量/成本/延迟）。

    - P50/P95：从 ``parsed.per_exam[*].latency_ms`` 取有效数值后按最近秩法算；
      样本 ≤1 时两值相等，空列表为 0；
    - 整体通过率 = successes / total（total=0 时取 0）；
    - 关键题通过率：**复用 eval_run 的 ``critical_pass_stats``**（对抗层全部 +
      典型层 critical=True；关键题 0 条时按 1.0 不设门槛）；
    - cost：取 ``parsed.cost``；token_total：取 ``parsed.token_usage.total``。

    Args:
        parsed: ``parse_promptfoo_results`` 输出。
        exam_sets: 评测体系 exam_sets（用于关键题口径）；缺省时关键题率退化为 1.0
            （无考题元数据，不设门槛）。

    Returns:
        ``{overall_rate, critical_rate, successes, failures, errors, cost, token_total,
        latency_p50_ms, latency_p95_ms, total, critical_total}``。
    """
    data = parsed if isinstance(parsed, dict) else {}
    per_exam = data.get("per_exam") if isinstance(data.get("per_exam"), list) else []

    latencies: list[float] = []
    for item in per_exam:
        if not isinstance(item, dict):
            continue
        latency = item.get("latency_ms")
        if isinstance(latency, bool) or not isinstance(latency, (int, float)):
            continue
        latencies.append(float(latency))
    latency_p50 = _percentile(latencies, 0.50)
    latency_p95 = _percentile(latencies, 0.95)

    successes = _safe_int(data.get("successes"))
    total = _safe_int(data.get("total"))
    overall_rate = (successes / total) if total > 0 else 0.0

    if isinstance(exam_sets, list) and exam_sets:
        critical_stats = critical_pass_stats(exam_sets, per_exam)
        critical_rate = critical_stats["rate"]
        critical_total = critical_stats["total"]
    else:
        # 无考题元数据：不设门槛（口径同 eval_run 关键题为 0 时取 1.0）
        critical_rate = 1.0
        critical_total = 0

    token_usage = data.get("token_usage") if isinstance(data.get("token_usage"), dict) else {}

    return {
        "overall_rate": overall_rate,
        "critical_rate": critical_rate,
        "successes": successes,
        "failures": _safe_int(data.get("failures")),
        "errors": _safe_int(data.get("errors")),
        "cost": _safe_float(data.get("cost")),
        "token_total": _safe_int(token_usage.get("total")),
        "latency_p50_ms": latency_p50,
        "latency_p95_ms": latency_p95,
        "total": total,
        "critical_total": critical_total,
    }
    # [C 2026-09-13 by codebuddy-ds41flash] 候选三维指标汇总纯函数（关键题口径复用 eval_run）


def _is_better(candidate: dict, incumbent: dict) -> bool:
    """比较两候选：整体通过率 > 关键题通过率 > 成本（低者胜）；全平不替换（保顺序）。"""
    if candidate["overall_rate"] != incumbent["overall_rate"]:
        return candidate["overall_rate"] > incumbent["overall_rate"]
    if candidate["critical_rate"] != incumbent["critical_rate"]:
        return candidate["critical_rate"] > incumbent["critical_rate"]
    if candidate["cost"] != incumbent["cost"]:
        return candidate["cost"] < incumbent["cost"]
    return False


def judge_bakeoff(candidates: list) -> dict:
    """纯函数：**代码硬判**推荐模型（模型不参与）。

    逐候选比较（取最优，全平保留更靠前者 = config 候选顺序）：
    1. 整体通过率最高者胜；
    2. 平局比关键题通过率（高者胜）；
    3. 再平局比成本（低者胜）；
    4. 仍平局按传入顺序靠前者胜（``_is_better`` 全平返回 False，不替换 incumbent）。

    Returns:
        ``{recommended_provider_id, recommended, candidates}``；candidates 为空时
        recommended_provider_id 与 recommended 均为 None。
    """
    items = [c for c in (candidates or []) if isinstance(c, dict)]
    if not items:
        return {
            "recommended_provider_id": None,
            "recommended": None,
            "candidates": [],
        }
    winner = items[0]
    for candidate in items[1:]:
        if _is_better(candidate, winner):
            winner = candidate
    return {
        "recommended_provider_id": winner.get("provider_id"),
        "recommended": winner,
        "candidates": items,
    }
    # [C 2026-09-13 by codebuddy-ds41flash] 推荐模型硬判纯函数（通过率>关键题率>成本>顺序）


def route_after_bake_off(state: dict) -> str:
    """条件边路由：按 model_selection.status 两态。

    - status in ("completed", "skipped") -> ``issue_splitting``（横跑完或人工跳过，均放行）；
    - 缺失/其他（工具错误在节点内部 interrupt，graph 不应拿到）-> ``bake_off``（保守自环重跑）。
    """
    selection = state.get("model_selection") or {}
    if isinstance(selection, dict) and selection.get("status") in ("completed", "skipped"):
        return "issue_splitting"
    return "bake_off"
    # [C 2026-09-13 by codebuddy-ds41flash] 对比选型两态条件边路由纯函数


def _skipped_selection(candidates_cfg: list, answer: object, ran_at: str) -> dict:
    """人工选择跳过横跑时的 model_selection（默认推荐项目自有 DeepSeek）。"""
    label = _DEFAULT_RECOMMENDED_PROVIDER_ID
    for cand in candidates_cfg:
        if str(cand.get("id")) == _DEFAULT_RECOMMENDED_PROVIDER_ID:
            label = str(cand.get("label") or _DEFAULT_RECOMMENDED_PROVIDER_ID)
            break
    return {
        "status": "skipped",
        "recommended": {
            "provider_id": _DEFAULT_RECOMMENDED_PROVIDER_ID,
            "label": label,
        },
        "skip_reason": str(answer if answer is not None else "").strip(),
        "ran_at": ran_at,
    }


def _summarize(stats: dict) -> dict:
    """把候选统计裁剪为落 state 的固定字段集合。"""
    return {field: stats.get(field) for field in _SUMMARY_FIELDS}


def make_bake_off(deps):
    """对比选型节点工厂：返回签名 (state: dict) -> dict 的节点函数（不调流水线模型）。

    仅 AI 核心需求执行；非真直接返回 ``{}``。
    依赖 ``deps.bake_off_config``（候选清单）与 ``deps.eval_tool["promptfoo_dir"]``；
    缺配置抛 NodeExecutionError。
    """

    def bake_off(state: dict) -> dict:
        # 1. 非 AI 核心需求直通（普通轨不应到本节点，到了也放行）
        if not state.get("ai_core"):
            return {}

        # 2. 上游产物缺失：不静默，抛错（同 eval_run 口径）
        eval_yaml_draft = state.get("eval_yaml_draft") or ""
        eval_archive = state.get("eval_archive") or {}
        eval_system = state.get("eval_system") or {}
        if not eval_yaml_draft or not eval_archive or not eval_system:
            raise NodeExecutionError(
                "bake_off",
                "缺少 eval_yaml_draft / eval_archive / eval_system，无法执行对比选型",
            )

        bake_off_config = getattr(deps, "bake_off_config", None)
        if not isinstance(bake_off_config, dict) or not bake_off_config.get("candidates"):
            raise NodeExecutionError(
                "bake_off",
                "未配置 bake_off.candidates，无法执行对比选型",
            )
        candidates_cfg = [
            cand
            for cand in (bake_off_config.get("candidates") or [])
            if isinstance(cand, dict) and cand.get("id")
        ]
        if not candidates_cfg:
            raise NodeExecutionError(
                "bake_off",
                "bake_off.candidates 无合法条目（缺 id），无法执行对比选型",
            )

        name = state.get("requirement_name") or "未命名需求"
        generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        candidate_payload = [
            {"id": str(cand["id"]), "label": str(cand.get("label") or cand["id"])}
            for cand in candidates_cfg
        ]

        # 3. 首次中断（await_decision）：候选清单 + 无历史选型记录说明；其他文本继续中断
        while True:
            answer = interrupt(
                {
                    "node": "bake_off",
                    "status": "await_decision",
                    "reason": (
                        "本版暂无历史选型记录：请确认是否对候选模型横跑对比。"
                        "横跑会真实产生多次模型调用（按候选数逐个跑同一批考题）。"
                    ),
                    "requirement_name": name,
                    "candidates": candidate_payload,
                }
            )
            decision = classify_bakeoff_answer(answer)
            if decision == "run":
                break
            if decision == "skip":
                # 4. 跳过：不调任何模型，默认推荐项目自有 DeepSeek，放行去 issue_splitting
                return {
                    "model_selection": _skipped_selection(
                        candidates_cfg, answer, generated_at
                    )
                }
            # 其他文本：不猜、不空转，继续中断等明确答复

        # 5. run：先确认 Promptfoo 执行器可用
        eval_tool = getattr(deps, "eval_tool", None)
        if not isinstance(eval_tool, dict) or not eval_tool.get("promptfoo_dir"):
            raise NodeExecutionError(
                "bake_off",
                "未配置 eval_tool.promptfoo_dir，无法定位 Promptfoo 执行器",
            )

        # 执行循环：任一工具错误 -> 中断，恢复后重跑本节点（重建配置、重跑候选）
        while True:
            # 5a. 逐候选生成单模型 YAML 并落盘到 output/<需求名>/评测/
            provider_plan: list[dict] = []
            build_failed = False
            for cand in candidates_cfg:
                provider_id = str(cand["id"])
                safe_id = sanitize_name(provider_id)
                config_text = build_provider_config(
                    eval_yaml_draft, provider_id, cand.get("kind")
                )
                if not config_text:
                    interrupt(
                        {
                            "node": "bake_off",
                            "status": "tool_error",
                            "reason": (
                                "评测配置生成失败：eval_yaml_draft 无法解析为合法 YAML"
                                f"（候选 {provider_id}）"
                            ),
                            "provider_id": provider_id,
                            "requirement_name": name,
                        }
                    )
                    build_failed = True
                    break
                config_path = deps.artifacts.save(
                    config_text, name, f"bakeoff-{safe_id}-eval_config", ext=".yaml"
                )
                provider_plan.append(
                    {
                        "candidate": cand,
                        "provider_id": provider_id,
                        "safe_id": safe_id,
                        "config_path": Path(config_path),
                    }
                )
            if build_failed:
                continue

            eval_dir = provider_plan[0]["config_path"].parent

            # 5b. 被测 system_prompt.txt 检查（await_prompt，口径同 eval_run）
            prompt_path = eval_dir / "system_prompt.txt"
            while not prompt_path.exists():
                interrupt(
                    {
                        "node": "bake_off",
                        "status": "await_prompt",
                        "reason": "评测目录缺少被测 system_prompt.txt，请放入后恢复",
                        "requirement_name": name,
                        "prompt_path": str(prompt_path.resolve()),
                    }
                )

            # 5c. 逐候选跑 Promptfoo；results 存 bakeoff-<safe_id>-results.json
            stats_list: list[dict] = []
            results_paths: list[str] = []
            tool_failed = False
            for item in provider_plan:
                returncode, stderr_tail, results_json = run_promptfoo_eval(
                    eval_tool["promptfoo_dir"],
                    item["config_path"],
                    eval_dir,
                    timeout=PROMPTFOO_TIMEOUT,
                )
                if returncode not in (EXIT_OK, EXIT_CONTENT_FAIL):
                    interrupt(
                        {
                            "node": "bake_off",
                            "status": "tool_error",
                            "reason": stderr_tail or f"Promptfoo 退出码 {returncode}",
                            "provider_id": item["provider_id"],
                            "requirement_name": name,
                        }
                    )
                    tool_failed = True
                    break
                if results_json is None:
                    raw_results_path = eval_dir / "results.json"
                    reason = (
                        "Promptfoo 退出码正常但未生成 results.json"
                        if not raw_results_path.exists()
                        else "results.json 无法解析为合法 JSON"
                    )
                    interrupt(
                        {
                            "node": "bake_off",
                            "status": "tool_error",
                            "reason": reason,
                            "provider_id": item["provider_id"],
                            "requirement_name": name,
                        }
                    )
                    tool_failed = True
                    break
                raw_text = (eval_dir / "results.json").read_text(encoding="utf-8")
                results_path = deps.artifacts.save(
                    raw_text, name, f"bakeoff-{item['safe_id']}-results", ext=".json"
                )
                results_paths.append(str(results_path))
                parsed = parse_promptfoo_results(results_json)
                stats = aggregate_candidate_stats(
                    parsed, eval_system.get("exam_sets") or []
                )
                stats["provider_id"] = item["provider_id"]
                stats["label"] = str(
                    item["candidate"].get("label") or item["provider_id"]
                )
                stats_list.append(stats)
            if tool_failed:
                continue
            break

        # 6~8. 硬判推荐 + 渲染报告 + 写 state
        selection = judge_bakeoff(stats_list)
        winner = selection["recommended"]

        model_selection = {
            "status": "completed",
            "ran_at": generated_at,
            "recommended": {
                "provider_id": winner["provider_id"],
                "label": winner["label"],
                "overall_rate": winner["overall_rate"],
                "critical_rate": winner["critical_rate"],
                "cost": winner["cost"],
                "latency_p50_ms": winner["latency_p50_ms"],
                "latency_p95_ms": winner["latency_p95_ms"],
            },
            "candidates": [_summarize(stats) for stats in stats_list],
        }

        report_md = deps.artifacts.render(
            "bakeoff_report.md.j2",
            {
                "requirement_name": name,
                "generated_at": generated_at,
                "selection": model_selection,
            },
        ).strip()
        report_path = deps.artifacts.save(
            report_md, name, "bakeoff_report", ext=".md"
        )

        # state 合并用默认 reducer 替换：节点内先读出现 dict，追加后整体写回
        new_archive = dict(eval_archive)
        new_archive["model_selection"] = model_selection

        bakeoff_artifacts = {
            "report_path": str(report_path),
            "config_paths": [str(item["config_path"]) for item in provider_plan],
            "results_paths": results_paths,
        }

        return {
            "model_selection": model_selection,
            "eval_archive": new_archive,
            "bakeoff_artifacts": bakeoff_artifacts,
        }

    return bake_off
    # [C 2026-09-13 by codebuddy-ds41flash] bake_off 节点主体完成（三态暂停 + 代码硬判，不自动空转）


# [C 2026-09-13 by codebuddy-ds41flash] nodes/bake_off.py 新增完成
