# RAG 子库 - 增量文档格式契约（frontmatter 解析 / 必填校验 / 纯文本自动包壳）
"""增量文档格式契约（《RAG知识库实施规划》§六 + 选型笔记「复审三」）。

契约（用户写的部分只有文首 YAML frontmatter）：

    ---
    title: 销售侧竞品分析          # 必填
    source: "https://... | 文件路径 | 用户手输"   # 必填，全局唯一来源键（幂等锚点）
    type: note | doc | snippet      # 必填
    updated: 2026-10-15             # 必填，YYYY-MM-DD
    tags: [竞品]                    # 可选
    ---

规则：
- 系统字段（doc_id / content_hash / strategy_version / chunk_index / heading_path）由管道写入，
  用户不写、这里也不解析；
- 无 frontmatter 的纯文本自动包壳：source 取内容 hash、type 取 note，标题按
  「显式 title → 首个 # 标题 → 调用方给的 title_fallback（如文件名）」回落；
- 有 frontmatter 时必填缺失（或 updated 不是 YYYY-MM-DD）→ 抛 DocumentFormatError，
  异常消息里带缺项清单，调用方直接透出，不静默吞；
- 显式传入的 title / source / type 覆盖 frontmatter（CLI 显式意图优先）；
  updated 参数只作 frontmatter 缺席时的缺省值（文件 mtime / 当天）。

本模块只做格式契约，不碰切片、不碰入库、不读磁盘。
"""
from __future__ import annotations

import datetime
import hashlib
import re

import yaml

# 必填字段（缺一即报错）与可选字段
REQUIRED_FIELDS: tuple[str, ...] = ("title", "source", "type", "updated")
OPTIONAL_FIELDS: tuple[str, ...] = ("tags",)
# 契约里的 type 取值（本期不做强校验：未知值原样放行，由调用方自行决定切片策略）
DOC_TYPES: tuple[str, ...] = ("note", "doc", "snippet")
# 自动包壳时的 type 缺省值
AUTO_TYPE = "note"
# 自动包壳且没有任何标题线索时的兜底标题
AUTO_TITLE = "未命名文档"
# 自动包壳的 source 取内容 hash 的前若干位（够唯一又可读）
HASH_SOURCE_CHARS = 16

# Markdown 1-6 级标题行（取首个非空标题作自动包壳的标题线索）
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.*)$")
# YYYY-MM-DD
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# 标题行尾的闭合 # 号
_TRAILING_HASH_RE = re.compile(r"\s*#+\s*$")


class DocumentFormatError(ValueError):
    """文档不满足增量格式契约（必填缺失 / 字段不合法）。

    Attributes:
        problems: 缺项清单，每项是字段名，或「字段(说明)」形态。
               str(exc) 已包含完整清单，CLI 可直接透出。
    """

    def __init__(self, problems, message: str | None = None):
        self.problems = [str(item) for item in (problems or [])]
        if message is None:
            message = (
                "文档不符合增量格式契约，缺项清单："
                + "；".join(self.problems)
            )
        super().__init__(message)


def split_front_matter(text) -> tuple[dict, str]:
    """拆出文首 YAML front matter：返回 (frontmatter 字典, 正文)。

    无 front matter（首行不是 ---）、未闭合、YAML 解析失败或顶层不是映射时，
    一律返回 ({}, 原文)，由调用方决定是否走自动包壳。
    """
    raw = str(text if text is not None else "")
    lines = raw.splitlines()
    if not lines or lines[0].lstrip("\ufeff").strip() != "---":
        return {}, raw
    for index in range(1, len(lines)):
        if lines[index].strip() != "---":
            continue
        try:
            data = yaml.safe_load("\n".join(lines[1:index]))
        except yaml.YAMLError:
            return {}, raw
        if not isinstance(data, dict):
            return {}, raw
        return data, "\n".join(lines[index + 1 :])
    return {}, raw


def content_hash(text) -> str:
    """文档内容 hash（sha256 全串，十六进制）：幂等跳过与自动包壳共用一个算法。"""
    return hashlib.sha256(str(text if text is not None else "").encode("utf-8")).hexdigest()


def auto_source(text) -> str:
    """纯文本自动包壳的 source：取内容 hash 前 16 位，形如 hash:0a1b2c3d4e5f6071。"""
    return f"hash:{content_hash(text)[:HASH_SOURCE_CHARS]}"


def first_heading(text) -> str:
    """取正文里第一个 Markdown 标题的文本；没有标题返回空串。"""
    for line in str(text if text is not None else "").splitlines():
        match = _HEADING_RE.match(line)
        if match:
            return _TRAILING_HASH_RE.sub("", match.group(1)).strip()
    return ""


def _first_nonempty(*values):
    """返回第一个「非空」的值；全空返回 None（空串与纯空白视为空）。"""
    for value in values:
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        return value
    return None


def _is_empty(value) -> bool:
    """值是否视为缺失（None 或纯空白字符串）。"""
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    return False


def normalize_updated(value) -> str | None:
    """updated 归一化为 YYYY-MM-DD 字符串；形态不合法返回 None。"""
    if value is None:
        return None
    if isinstance(value, datetime.datetime):
        return value.date().isoformat()
    if isinstance(value, datetime.date):
        return value.isoformat()
    text = str(value).strip()
    if _DATE_RE.match(text):
        return text
    # 兼容「2026-10-15 10:00:00」这类带时间的写法
    if len(text) >= 10 and _DATE_RE.match(text[:10]):
        return text[:10]
    return None


def normalize_tags(value) -> list[str]:
    """tags 归一化为字符串列表：支持 YAML 列表、逗号/顿号分隔的字符串、空值。"""
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    if not text:
        return []
    parts = re.split(r"[,，、]", text)
    return [part.strip() for part in parts if part.strip()]


# 检索元数据（可选，Three.js/WebGPU 预设方案 2.2）：阶段路由、版本有效性、约束类型等。
# Chroma 元数据只收标量（str/int/float/bool），列表一律归一化为逗号串，检索侧按逗号拆。
RETRIEVAL_META_FIELDS: tuple[str, ...] = (
    "phase_tags",
    "valid_for_versions",
    "invalid_after",
    "constraint_type",
    "entity_type",
    "weight",
    "last_verified",
    "why_not_the_obvious_way",
    "evidence_source",
    "evidence_url",
    "evidence_date",
    "evidence_level",
)
# constraint_type / entity_type 参考取值（不做强校验，未知值原样放行）
CONSTRAINT_TYPES: tuple[str, ...] = ("hard", "soft", "negative")
ENTITY_TYPES: tuple[str, ...] = ("api", "param", "pattern", "failure", "script")


def _retrieval_meta(front: dict) -> dict:
    """从 frontmatter 提取检索元数据（全部 Chroma 安全的标量）。

    evidence 可写成嵌套映射（evidence: {source, url, date, level}）或扁平键
    （evidence_source 等），两种写法等价。weight 非数字时置 None（由入库侧丢弃）。
    """
    front = dict(front or {})
    evidence = front.get("evidence") if isinstance(front.get("evidence"), dict) else {}

    def _csv(value) -> str:
        return ",".join(normalize_tags(value))

    def _text(value) -> str:
        return "" if value is None else str(value).strip()

    try:
        weight = float(front.get("weight"))
    except (TypeError, ValueError):
        weight = None

    return {
        "phase_tags": _csv(front.get("phase_tags")),
        "valid_for_versions": _csv(front.get("valid_for_versions")),
        "invalid_after": _text(front.get("invalid_after")),
        "constraint_type": _text(front.get("constraint_type")),
        "entity_type": _text(front.get("entity_type")),
        "weight": weight,
        "last_verified": normalize_updated(front.get("last_verified")) or "",
        "why_not_the_obvious_way": _text(front.get("why_not_the_obvious_way")),
        "evidence_source": _text(evidence.get("source") or front.get("evidence_source")),
        "evidence_url": _text(evidence.get("url") or front.get("evidence_url")),
        "evidence_date": normalize_updated(evidence.get("date") or front.get("evidence_date"))
        or "",
        "evidence_level": _text(evidence.get("level") or front.get("evidence_level")),
    }


def _nonempty_retrieval(front: dict) -> dict:
    """检索元数据里非空的键（空值不入库，老文档的 metadata 形态不变）。"""
    return {
        key: value
        for key, value in _retrieval_meta(front).items()
        if value not in ("", None)
    }


def parse_document(
    text,
    *,
    title=None,
    source=None,
    doc_type=None,
    updated=None,
    title_fallback=None,
) -> dict:
    """按增量格式契约解析一篇文档，返回归一化元数据。

    Args:
        text: 文档全文（可含 frontmatter）。
        title / source / doc_type: 显式指定值，覆盖 frontmatter 里的同名字段。
        updated: 只用于无 frontmatter 的自动包壳（文件 mtime / 当天）；
            有 frontmatter 时 updated 必须由文档自己写，缺了按必填缺失报错，
            不给静默兜底（否则「必填」形同虚设）。
        title_fallback: 无 frontmatter 且没有标题线索时的兜底标题（如文件名）。

    Returns:
        {
          "title": str, "source": str, "type": str, "updated": "YYYY-MM-DD",
          "tags": [str], "auto_wrapped": bool, "body": str,
        }

    Raises:
        DocumentFormatError: 有 frontmatter 但必填缺失 / updated 形态不合法，
            异常消息与 problems 均为缺项清单。
    """
    raw = str(text if text is not None else "")
    front, body = split_front_matter(raw)

    if not front:
        # 无 frontmatter：纯文本自动包壳（source=内容 hash、type=note），零格式负担
        return {
            "title": str(
                _first_nonempty(title, first_heading(body), title_fallback, AUTO_TITLE)
            ).strip(),
            "source": str(_first_nonempty(source, auto_source(raw))).strip(),
            "type": str(_first_nonempty(doc_type, AUTO_TYPE)).strip(),
            "updated": normalize_updated(_first_nonempty(updated, datetime.date.today().isoformat())),
            "tags": normalize_tags(front.get("tags")),
            "auto_wrapped": True,
            "body": body,
            **_nonempty_retrieval(front),
        }

    problems: list[str] = []
    title_value = _first_nonempty(title, front.get("title"))
    source_value = _first_nonempty(source, front.get("source"))
    type_value = _first_nonempty(doc_type, front.get("type"))
    updated_value = front.get("updated")

    for field, value in (
        ("title", title_value),
        ("source", source_value),
        ("type", type_value),
        ("updated", updated_value),
    ):
        if _is_empty(value):
            problems.append(field)

    updated_text = normalize_updated(updated_value)
    if not _is_empty(updated_value) and updated_text is None:
        problems.append(f"updated(应为 YYYY-MM-DD，实际 {updated_value!r})")

    if problems:
        raise DocumentFormatError(problems)

    return {
        "title": str(title_value).strip(),
        "source": str(source_value).strip(),
        "type": str(type_value).strip(),
        "updated": updated_text,
        "tags": normalize_tags(front.get("tags")),
        "auto_wrapped": False,
        "body": body,
        **_nonempty_retrieval(front),
    }
