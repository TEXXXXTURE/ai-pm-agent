# 出题质量机械检查：材料可投喂 + 评分可核对
"""eval_design 产物的出题质量机械检查（纯函数，只提示不阻断）。

纯函数 ``audit_exam_quality(eval_system) -> {"errors": [], "warnings": [], "notes": []}``，
逐条给出「题号 + 问题 + 建议」：

- ``errors``   结构性硬问题（replay 层必须空数组 + placeholder_note 非空）；
- ``warnings`` 建议类问题（实料篇幅不足 / 占位符样式 / assertion 只押单个词 /
               llm_judge 缺 judge_rubric 或判据过短）；
- ``notes``    汇总一行，或检查自身异常时的一条「检查未执行：<原因>」。

口径（对齐 S048 任务书 2.2）：
- **只提示、不阻断、不改任何及格线、不替模型改题**；
- 检查函数自身异常不得让 ``eval_design`` 节点崩（捕获后记一条「检查未执行」）。

起因：构建期评测连续出现「材料没法投喂」（场景描述当材料、占位符材料）与
「评分方式与实际回答对不上」（断言只押一个词，合格回答的等价写法被判不通过）。
"""
from __future__ import annotations

import re

# 典型层（以及非「考输入不合格」的边界题）的实料篇幅门槛：低于该字数即 warning。
# 口径来自 S048 任务书：typical 层正文 ≥1000 字，至少要越过产品自身设定的可读性门槛。
TYPICAL_MIN_MATERIAL_CHARS = 1000

# 占位符兜底：prompt_hint 短于该字数且以「输入」开头，基本等于没给材料。
SHORT_HINT_CHARS = 60

# llm_judge 的 judge_rubric 判据字数下限（低于该值判「写不清观察点」）。
JUDGE_RUBRIC_MIN_CHARS = 20

# 「只押单个词」判定的长度上限：超过该长度视为整句期望，不再按单词口径提示。
SINGLE_TERM_MAX_CHARS = 15

# 边界层的「考输入不合格」标记：命中即认为该题刻意给短材料/无结构文本，免篇幅检查。
_INVALID_INPUT_MARKERS: tuple[str, ...] = (
    "不合格",
    "无法解析",
    "解析失败",
    "损坏",
    "乱码",
    "扫描件",
    "扫描版",
    "非文本",
    "不支持",
    "空文件",
    "缺字段",
    "无法拆键值",
    "超短",
    "残缺",
    "非法",
    "格式不符",
    "拒绝解析",
)

# 占位符样式（连续省略号 / 方括号占位 / XX 占位）。
_PLACEHOLDER_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\.{3,}"),  # 连续 ASCII 点
    re.compile(r"…{2,}"),  # 中文省略号
    re.compile(r"。{3,}"),  # 连续句号
    re.compile(r"[Xx]{2,}"),  # XX 占位
    re.compile(r"\[\s*\.{0,3}\s*\]"),  # [] / [...] / [..]
    re.compile(r"【\s*…*\s*】"),  # 【】/【…】
)

# 占位符关键词（任务书点名：粘贴 / 示例材料）。
_PLACEHOLDER_LITERALS: tuple[str, ...] = ("粘贴", "示例材料")

# 「粘贴」的例外：主语 + 粘贴（「作者粘贴以下内容」）是真实场景描述，不是占位写法；
# 真机实证：某边界题材料本身写「作者粘贴以下内容，并说：帮我全部解析入库」，被误判为占位。
# 只有去掉这类主语框架后仍出现「粘贴」，才算占位指令（如「请把转写稿粘贴到这里」）。
_PASTE_EVENT_SUBJECTS: tuple[str, ...] = ("作者粘贴", "用户粘贴", "我粘贴", "他粘贴", "她粘贴")

# assertion 的「任一/或」写法标记：出现任一即视为已覆盖多种合格表述。
_ALT_MARKERS: tuple[str, ...] = ("|", "/", "、", "，", ",", "；", ";", "或", "任一")


def _exam_material_text(exam: dict) -> str:
    """取边界题「是否刻意给不合格输入」的判据文本（description + prompt_hint）。"""
    return f"{exam.get('description') or ''}\n{exam.get('prompt_hint') or ''}"


def _is_invalid_input_boundary(exam: dict) -> bool:
    """边界题是否属于「考输入不合格」：命中标记词即免篇幅检查。"""
    text = _exam_material_text(exam)
    return any(marker in text for marker in _INVALID_INPUT_MARKERS)


def _has_bare_paste(text: str) -> bool:
    """「粘贴」是否为占位指令：去掉「作者粘贴」这类主语框架后仍出现才算。"""
    remain = text
    for subject in _PASTE_EVENT_SUBJECTS:
        remain = remain.replace(subject, "")
    return "粘贴" in remain


def _placeholder_hit(prompt_hint: str) -> str:
    """返回命中的占位符样式（无命中返回空串）。

    Returns:
        命中的模式描述（如 ``连续省略号`` / ``粘贴``），无命中返回 ``""``。
    """
    labels = ("连续省略号", "中文省略号", "连续句号", "XX 占位", "方括号占位", "空书名号")
    for label, pattern in zip(labels, _PLACEHOLDER_PATTERNS):
        if pattern.search(prompt_hint):
            return label
    for literal in _PLACEHOLDER_LITERALS:
        if literal not in prompt_hint:
            continue
        if literal == "粘贴" and not _has_bare_paste(prompt_hint):
            continue
        return literal
    if len(prompt_hint) < SHORT_HINT_CHARS and prompt_hint.lstrip().startswith("输入"):
        return "以「输入」开头的场景描述"
    return ""


def _is_single_term_assertion(assertion: str) -> bool:
    """assertion 是否形如 ``contains: 某词`` 且只押一个词、无「任一/或」写法。"""
    text = str(assertion or "").strip()
    if not text.lower().startswith("contains:"):
        return False
    body = text[len("contains:"):].strip()
    if not body:
        return False
    if any(marker in body for marker in _ALT_MARKERS):
        return False
    if len(body) > SINGLE_TERM_MAX_CHARS:
        return False
    return not re.search(r"\s", body)


def _iter_exams(eval_system: dict):
    """逐条产出 (层名, 题 dict)；非 dict 结构安全跳过。"""
    for exam_set in eval_system.get("exam_sets") or []:
        if not isinstance(exam_set, dict):
            continue
        layer = str(exam_set.get("layer") or "")
        for exam in exam_set.get("exams") or []:
            if isinstance(exam, dict):
                yield layer, exam


def _audit_replay_layers(eval_system: dict) -> list[str]:
    """检查项 5：replay 层必须为空数组 + placeholder_note 非空（现状校验，保留）。"""
    errors: list[str] = []
    found = False
    for exam_set in eval_system.get("exam_sets") or []:
        if not isinstance(exam_set, dict):
            continue
        if str(exam_set.get("layer") or "") != "replay":
            continue
        found = True
        exams = exam_set.get("exams")
        count = len(exams) if isinstance(exams, list) else 0
        if count:
            errors.append(
                f"replay：本期必须为空数组占位，实际 {count} 条；"
                "建议清空，真实坏例等第 11 段运营数据回流填充。"
            )
        if not str(exam_set.get("placeholder_note") or "").strip():
            errors.append(
                "replay：placeholder_note 为空；"
                "建议写明「本期为空占位；由第 11 段运营数据回流填充真实坏例」。"
            )
    if not found:
        errors.append(
            "replay：未找到 replay 层考题集；建议按四层结构补上 replay 层（空数组 + placeholder_note）。"
        )
    return errors


def _audit_exam(exam: dict, layer: str) -> tuple[list[str], list[str]]:
    """检查单道题（检查项 1-4），返回 (errors, warnings)。"""
    errors: list[str] = []
    warnings: list[str] = []
    exam_id = str(exam.get("id") or "(无编号)")
    prompt_hint = str(exam.get("prompt_hint") or "")
    scorer = str(exam.get("scorer") or "")

    # 检查项 1：实料篇幅（typical 层 + 非「考输入不合格」的边界题）
    if layer in ("typical", "boundary"):
        exempt = layer == "boundary" and _is_invalid_input_boundary(exam)
        if not exempt and len(prompt_hint) < TYPICAL_MIN_MATERIAL_CHARS:
            warnings.append(
                f"{exam_id}：prompt_hint 仅 {len(prompt_hint)} 字，"
                f"不足实料门槛 {TYPICAL_MIN_MATERIAL_CHARS} 字；"
                "建议补足能直接投喂的正文材料（材料不足时被测模型会按可读性门槛停下，该题考不出内容），"
                "若该题本就是短材料题，请在 description 写明「考输入不合格」。"
            )

    # 检查项 2：占位符样式（这类基本等于没给材料）
    hit = _placeholder_hit(prompt_hint)
    if hit:
        warnings.append(
            f"{exam_id}：prompt_hint 疑似占位写法（命中「{hit}」）；"
            "建议换成可直接投喂的完整实料，删掉省略号、「粘贴」「示例材料」「XX」等占位写法。"
        )

    # 检查项 3：assertion 只押单个词、无「任一/或」写法
    if scorer != "llm_judge":
        assertion = str(exam.get("assertion") or "")
        if _is_single_term_assertion(assertion):
            warnings.append(
                f"{exam_id}：assertion「{assertion}」只押单个词，"
                "合格回答的等价写法会被判不通过；"
                "建议改成「任一」写法（如 regex: 无法|不能|拒绝执行|需你确认），"
                "或改用 llm_judge 并在 judge_rubric 写清观察点。"
            )

    # 检查项 4：llm_judge 题缺 judge_rubric 或判据过短
    if scorer == "llm_judge":
        rubric = str(exam.get("judge_rubric") or "").strip()
        if len(rubric) < JUDGE_RUBRIC_MIN_CHARS:
            warnings.append(
                f"{exam_id}：llm_judge 题 judge_rubric 缺失或仅 {len(rubric)} 字"
                f"（少于 {JUDGE_RUBRIC_MIN_CHARS} 字）；"
                "建议写清「出现什么算通过、出现什么算不通过」，让人工能照着复核。"
            )

    return errors, warnings


def audit_exam_quality(eval_system: dict) -> dict:
    """机械检查一套考题集：材料是否可投喂 + 评分方式是否可核对。

    Args:
        eval_system: EvalDesignSchema 同构 dict（四层 exam_sets + pass_lines）。

    Returns:
        ``{"errors": [...], "warnings": [...], "notes": [...]}``：
        每条 finding 为「题号 + 问题 + 建议」的一句话；
        ``notes`` 始终含一行检查汇总，检查自身异常时只含一条「检查未执行：<原因>」。

    Note:
        **只提示、不阻断**：返回结果不参与任何及格线判定与路由，也不修改考题内容。
        检查函数自身异常不外抛（节点接线处另有兜底捕获）。
    """
    if not isinstance(eval_system, dict):
        return {
            "errors": [],
            "warnings": [],
            "notes": [
                f"检查未执行：eval_system 不是 dict（实际 {type(eval_system).__name__}）"
            ],
        }

    errors: list[str] = []
    warnings: list[str] = []
    layer_counts: dict[str, int] = {}
    try:
        errors.extend(_audit_replay_layers(eval_system))
        for layer, exam in _iter_exams(eval_system):
            layer_counts[layer] = layer_counts.get(layer, 0) + 1
            try:
                exam_errors, exam_warnings = _audit_exam(exam, layer)
            except Exception as exc:  # 单题检查出错不影响其余题
                notes_hint = f"{exam.get('id') or '(无编号)'}：该题检查出错（{exc}）"
                warnings.append(notes_hint)
                continue
            errors.extend(exam_errors)
            warnings.extend(exam_warnings)
    except Exception as exc:  # 检查函数自身异常：返回「检查未执行」，不抛给节点
        return {
            "errors": [],
            "warnings": [],
            "notes": [f"检查未执行：{type(exc).__name__}: {exc}"],
        }

    total = sum(layer_counts.values())
    counts_text = " / ".join(
        f"{layer} {layer_counts.get(layer, 0)}"
        for layer in ("typical", "boundary", "adversarial", "replay")
    )
    notes = [
        f"共检查 {total} 道题（{counts_text}）："
        f"errors {len(errors)} 条，warnings {len(warnings)} 条。"
    ]
    return {"errors": errors, "warnings": warnings, "notes": notes}


