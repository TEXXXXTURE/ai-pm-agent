# 总结质量机械检查（纯函数，只提示不阻断）
"""行业追踪产物的机械质量检查（纯函数，只提示不阻断）。

检查对象两类：
- ``audit_digest_quality(md_text)``：每期总结（给人读的一篇）——规则 27 条里
  带实测阈值的 11 条（见 ``docs/S052-R13第三步第四步-默认人设、编写规则与反例.md`` 第五节）；
- ``audit_entry_fields(entry)``：单条条目（知识库层）——字段齐全性与防编造约束
  （见 ``docs/S052-R13分析规则-判断动作与条目结构.md`` 第九、十节）。

口径（对齐 eval_quality.py）：
- **只提示、不阻断**：结果不参与任何判定与路由，也不修改产物；
- 检查函数自身异常不外抛，记一条「检查未执行」。

阈值来源全部是 S052 实测（11 篇好文档样本），不是拍脑袋：
好文档 1,565–6,748 字（中位约 4.7k），破折号 0.5–1.5/千字，作者在场 5.9–19.5/千，
提问 1.2–1.8/千，点名具体系统 24.7–89.4/千。我们模型产出的差距与超标见同文件。
"""
from __future__ import annotations

import re

# ── 每期总结（给人读）阈值（S052 第五节） ──────────────────────────────

MIN_CHARS = 2000          # 单篇 2000–4000 字
MAX_CHARS = 4000
DASH_PER_1000_MAX = 2.0   # 破折号 ≤2 处/千字
AUTHOR_PRESENCE_MIN = 3   # "我/我们" ≥3 处
QUESTION_MIN = 1          # 设问 ≥1 处
ENTITY_PER_1000_MIN = 10.0  # 具体专名 ≥10 处/千字
NUMBER_WITH_UNIT_MIN = 1   # 带单位对照数字 ≥1 处
LONG_SENTENCE_RATIO = 3.0  # 最长句 ≥3 倍中位句长
SHORT_SENTENCE_MAX = 8     # 最短句 ≤8 字
HEADING_PER_1000_MAX = 8.0  # 小标题 ≤8 个/千字

# 禁用连接词（出现即违规，0 容忍）
_BANNED_CONNECTIVES: tuple[str, ...] = (
    "首先", "其次", "此外", "最后", "综上所述", "总而言之", "值得注意的是", "不难看出",
)
# 一级黑词（出现即违规，0 容忍）
_LEVEL1_WORDS: tuple[str, ...] = (
    "赋能", "助力", "闭环", "打通", "抓手", "对齐", "颗粒度",
    "可谓是", "无疑是", "堪称", "当之无愧",
)
# 二级词（每段 ≤1 次）
_LEVEL2_WORDS: tuple[str, ...] = (
    "显著提升", "大幅改善", "极大增强",
)

_EM_DASH_RE = re.compile(r"——|—")
_QUESTION_RE = re.compile(r"[？?]")
# 具体专名粗判：含大写字母/数字的连续片段（模型爱写"主流方案"这类无专名表述）
_ENTITY_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.\-]{1,}|\d+[A-Za-z]+")
_NUMBER_WITH_UNIT_RE = re.compile(r"\d+(?:\.\d+)?\s*(?:ms|ms|秒|毫秒|倍|%|GB|MB|KB|TB|token|万|亿|美元|元|核|个|条|台)")
# 设问：问号结尾的句子（中文问句可无问号，这里只保守判问号）
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[。！？!?])|(?<=[。！？!?])[\"\u201d]")
_HEADING_RE = re.compile(r"^#{1,6}\s+.+$", re.MULTILINE)


def _count_chars(text: str) -> int:
    """正文字数：去掉小标题、空白后的中文字符 + 数字字母（近似，与实测同口径）。"""
    body = _HEADING_RE.sub("", text)
    # 去掉 markdown 语法符号，保留中英数
    cleaned = re.sub(r"[#*`>\-\|\[\]\(\)]", "", body)
    return len(re.sub(r"\s", "", cleaned))


def _split_sentences(text: str) -> list[str]:
    parts = _SENTENCE_SPLIT_RE.split(text)
    return [p.strip() for p in parts if p.strip()]


def audit_digest_quality(md_text: str) -> dict:
    """机械检查每期总结（给人读的那一篇）。

    Args:
        md_text: 总结的 Markdown 全文。

    Returns:
        ``{"errors": [], "warnings": [], "notes": []}``，全部 finding 一句话；
        ``notes`` 含一行汇总；检查自身异常时只含一条「检查未执行」。
    """
    if not isinstance(md_text, str) or not md_text.strip():
        return {
            "errors": [], "warnings": [],
            "notes": ["检查未执行：md_text 为空或不是字符串"],
        }
    warnings: list[str] = []
    text = md_text

    # 1. 篇幅
    chars = _count_chars(text)
    if chars < MIN_CHARS:
        warnings.append(f"单篇 {chars} 字，低于 {MIN_CHARS} 字；建议写到 2–4k，砍不掉就拆进知识库。")
    elif chars > MAX_CHARS:
        warnings.append(f"单篇 {chars} 字，超过 {MAX_CHARS} 字；多半在复述材料，建议砍到 2–4k。")

    # 2. 破折号密度
    total_1000 = chars / 1000.0
    dash_count = len(_EM_DASH_RE.findall(text))
    dash_per_1000 = dash_count / total_1000 if total_1000 > 0 else 0
    if dash_per_1000 > DASH_PER_1000_MAX:
        warnings.append(
            f"破折号 {dash_count} 处（{dash_per_1000:.1f}/千字），超过 {DASH_PER_1000_MAX}/千字；"
            "这是模型最明显的指纹（人写 0.5–1.5），建议改逗号、括号或另起一句。"
        )

    # 3. 作者在场
    author_count = len(re.findall(r"我|我们", text))
    if author_count < AUTHOR_PRESENCE_MIN:
        warnings.append(
            f"「我/我们」仅 {author_count} 处，少于 {AUTHOR_PRESENCE_MIN} 处；"
            "作者不在场，读者不知道判断是谁的、凭什么，至少 3 处责任句。"
        )

    # 4. 设问
    question_count = len(_QUESTION_RE.findall(text))
    if question_count < QUESTION_MIN:
        warnings.append(
            f"设问 {question_count} 处，少于 {QUESTION_MIN} 处；"
            "人写会设问、会自己找茬，至少 1 处放在「讲完旧做法、要给新做法定性」之前。"
        )

    # 5. 具体专名
    entity_count = len(_ENTITY_RE.findall(text))
    entity_per_1000 = entity_count / total_1000 if total_1000 > 0 else 0
    if entity_per_1000 < ENTITY_PER_1000_MIN:
        warnings.append(
            f"具体专名约 {entity_count} 处（{entity_per_1000:.1f}/千字），低于 {ENTITY_PER_1000_MIN}/千字；"
            "含糊是这类文章最大的破绽，点名具体系统/产品/版本并给来源。"
        )

    # 6. 带单位对照数字
    unit_numbers = _NUMBER_WITH_UNIT_RE.findall(text)
    if len(unit_numbers) < NUMBER_WITH_UNIT_MIN:
        warnings.append(
            f"带单位的数字仅 {len(unit_numbers)} 处，少于 {NUMBER_WITH_UNIT_MIN} 处；"
            "给对照数字（从 200ms 降到 40ms、便宜 3 倍），读得出变化多大。"
        )

    # 7. 句长起伏（最长句 ≥3 倍中位句长）
    sents = _split_sentences(text)
    if len(sents) >= 3:
        lengths = sorted(len(s) for s in sents)
        median = lengths[len(lengths) // 2] if lengths else 0
        longest = max(lengths) if lengths else 0
        if median > 0 and longest / median < LONG_SENTENCE_RATIO:
            warnings.append(
                f"最长句 {longest} 字 / 中位 {median} 字 = {longest / median:.1f}，"
                f"低于 {LONG_SENTENCE_RATIO} 倍；句长没起伏，读起来像表格展开。"
            )

    # 8. 短句存在（≤8 字）
    short_sents = [s for s in sents if len(s) <= SHORT_SENTENCE_MAX]
    if not short_sents:
        warnings.append(f"没有 ≤{SHORT_SENTENCE_MAX} 字的短句；句长要有起伏，至少一句成段。")

    # 9. 小标题密度
    headings = len(_HEADING_RE.findall(text))
    heading_per_1000 = headings / total_1000 if total_1000 > 0 else 0
    if heading_per_1000 > HEADING_PER_1000_MAX:
        warnings.append(
            f"小标题 {headings} 个（{heading_per_1000:.1f}/千字），超过 {HEADING_PER_1000_MAX}/千字；"
            "结构虚胖，并进上一节或写实。"
        )

    # 10. 禁用连接词（0 容忍）
    for w in _BANNED_CONNECTIVES:
        if w in text:
            warnings.append(f"出现禁用连接词「{w}」：没有信息量的过渡，删掉，结论直接说。")

    # 11. 黑词（一级 0 容忍，二级每段 ≤1）
    for w in _LEVEL1_WORDS:
        if w in text:
            warnings.append(f"出现一级黑词「{w}」：把具体的事压成抽象名词，说清谁、做了什么、结果是什么。")
    for w in _LEVEL2_WORDS:
        n = text.count(w)
        if n > 0:
            warnings.append(f"二级词「{w}」出现 {n} 次：没有量，读不出变化多大，给对照数字。")

    notes = [
        f"共检查 {len(warnings)} 项（{chars} 字、{len(sents)} 句、{headings} 小标题）："
        f"warnings {len(warnings)} 条。"
    ]
    return {"errors": [], "warnings": warnings, "notes": notes}


# ── 单条条目（知识库层）字段检查 ────────────────────────────────────────

_ENTRY_REQUIRED_FIELDS: tuple[tuple[str, str], ...] = (
    ("title", "标题"), ("one_liner", "一句话"), ("prod_usage", "① 生产在用的方案"),
    ("community_popular", "② 社区原本的流行方案"), ("what_changed", "③ 这个新东西改了什么"),
    ("innovation_type", "创新属于哪一类"), ("boundary", "拓开的边界"), ("landing", "能不能落地"),
    ("gap", "补的是我们哪条缺口"), ("priority", "优先级判定"), ("significance", "对我们的意义"),
    ("evidence_tier", "证据等级"), ("link", "原文链接"),
)
# 防编造：出现即提示（不阻断）
_FABRICATION_MARKERS: tuple[str, ...] = (
    "传统方法", "首个", "首次", "创新", "颠覆", "革命性",
)


def audit_entry_fields(entry: dict) -> dict:
    """机械检查知识库层单条条目：字段齐全性 + 防编造表述。

    Args:
        entry: 单条条目 dict（title / one_liner / prod_usage / community_popular /
            what_changed / innovation_type / boundary / landing / gap / priority /
            significance / evidence_tier / link）。

    Returns:
        ``{"errors": [], "warnings": [], "notes": []}``。
    """
    if not isinstance(entry, dict):
        return {"errors": [], "warnings": [], "notes": ["检查未执行：entry 不是 dict"]}
    warnings: list[str] = []
    title = str(entry.get("title") or "无标题")
    missing = [label for key, label in _ENTRY_REQUIRED_FIELDS if not str(entry.get(key) or "").strip()]
    if missing:
        warnings.append(f"条目「{title}」缺字段：{'、'.join(missing)}")
    text = "\n".join(str(entry.get(k) or "") for k, _ in _ENTRY_REQUIRED_FIELDS)
    for marker in _FABRICATION_MARKERS:
        if marker in text:
            warnings.append(f"条目「{title}」出现「{marker}」："
                            "若是作者自述请标「作者自述」；说不出改掉哪个假设就不许用。")
    notes = [f"条目字段检查：warnings {len(warnings)} 条。"]
    return {"errors": [], "warnings": warnings, "notes": notes}


