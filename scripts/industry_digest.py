#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""industry_digest.py —— R13 行业追踪：读信源库新文章 → 提炼条目进知识库 → 写每期总结

对应规范：
- 分析规则/判断动作：docs/S052-R13分析规则-判断动作与条目结构.md
- 知识结构（目录四块、一条一文件、七个话题、更新规则）：docs/S052-R13知识结构草案.md
- 默认人设/编写规则/反例：docs/S052-R13第三步第四步-默认人设、编写规则与反例.md
- 阅读 SOP（可编辑提示词）：src/components/prompts/industry_digest.md

数据来源（只读，不写库）：
- 默认路径见 `DEFAULT_SOURCE_DB`（可在 config.yaml `industry_digest.source_db` 覆盖，或用 `--source-db` 指定）——信源库是收集层，
  本脚本只读它最近抓取的文章，绝不改写库结构或内容。

模型：
- 走项目统一层 kernel.model（config.yaml 的 llm.providers），产品不内置任何 key；
- 分两档：判类/提炼条目用便宜模型（provider 可指定），
  写每期总结用大上下文模型（provider 可指定）。

产物（默认落 output/行业追踪/，可 --output 指定）：
- 索引.md / 缺口清单.md / 更新日志.md（append）/ 话题/<话题>/<条目>.md /
  每期总结/<日期>-第NN期.md
- 质量检查（guards/digest_quality.py）只提示不阻断，warnings 写进日志行。

用法：
  bash scripts/run-tool.sh scripts/industry_digest.py                # 默认近7天
  bash scripts/run-tool.sh scripts/industry_digest.py --days 3
  bash scripts/run-tool.sh scripts/industry_digest.py --provider-classify deepseek --provider-write deepseek
  bash scripts/run-tool.sh scripts/industry_digest.py --dry-run      # 只列候选文章，不调模型

退出码：0 正常；1 运行错误（信源库不可达/模型未配置）；2 参数错误。
"""
from __future__ import annotations

import argparse
import datetime
import email.utils
import json
import os
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any, Callable

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 加入 src 到导入路径（PYTHONPATH 未设置时脚本也能直接跑）
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from kernel.config import load_config  # noqa: E402
from kernel.model import build_llm  # noqa: E402

# ── 默认路径（可在 config.yaml 覆盖，见 load_digest_config） ──────────────
DEFAULT_SOURCE_DB = PROJECT_ROOT / "data" / "sources" / "articles.db"
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "output" / "行业追踪"
DEFAULT_PROMPT_PATH = SRC / "components" / "prompts" / "industry_digest.md"
DEFAULT_PROVIDER_CLASSIFY = "deepseek"
DEFAULT_PROVIDER_WRITE = "deepseek"
DEFAULT_DAYS = 7
# 判类/提炼用便宜模型时，上下文小，条目一次最多送多少条
CLASSIFY_MAX_ARTICLES = 40
# 每期总结最多覆盖多少条（写总结用大模型，控制在上下文内）
WRITE_MAX_ENTRIES = 30

# 七个固定话题（知识结构草案已定）
TOPICS: tuple[str, ...] = (
    "模型与大模型", "Agent与工具", "检索与知识库", "评测", "成本与部署", "合规与监管", "产品与商业",
)

# 信源库 search.py 输出契约同构（只读 SQL 直接取，不调 CLI，避免子进程）
_ARTICLES_SQL = """
SELECT title, link, author, published, summary, content, source_id
FROM articles
WHERE fetched_at >= ?
ORDER BY fetched_at DESC
LIMIT ?
"""


# ── 配置 ──────────────────────────────────────────────────────────────


def load_digest_config() -> dict[str, Any]:
    """读取 config.yaml 的 industry_digest 段（没有则用默认值）。"""
    cfg = load_config()
    section = cfg.get("industry_digest", {})
    return {
        "source_db": Path(str(section.get("source_db", DEFAULT_SOURCE_DB))),
        "output_root": Path(str(section.get("output_root", DEFAULT_OUTPUT_ROOT))),
        "prompt_path": Path(str(section.get("prompt_path", DEFAULT_PROMPT_PATH))),
        "provider_classify": section.get("provider_classify", DEFAULT_PROVIDER_CLASSIFY),
        "provider_write": section.get("provider_write", DEFAULT_PROVIDER_WRITE),
        "days": int(section.get("days", DEFAULT_DAYS)),
    }


# ── 数据读取（只读信源库） ─────────────────────────────────────────────


def fetch_recent_articles(db_path: Path, days: int, limit: int = CLASSIFY_MAX_ARTICLES) -> list[dict]:
    """读信源库最近 N 天抓取的文章（只读）。

    Args:
        db_path: articles.db 路径。
        days: 近 N 天（按 fetched_at）。
        limit: 最多取多少条（判类阶段一次喂给模型的上限）。

    Returns:
        按 fetched_at 倒序的 article dict 列表（title/link/author/published/summary/
        content/source_id/source_name）。

    Raises:
        FileNotFoundError: 信源库不存在（调用方转成友好报错）。
    """
    if not db_path.is_file():
        raise FileNotFoundError(f"信源库不存在：{db_path}\n请确认信源库已抓取，或用 --source-db 指定路径。")

    since = datetime.datetime.now() - datetime.timedelta(days=days)
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(_ARTICLES_SQL, (since.isoformat(sep=" "), limit)).fetchall()
    finally:
        con.close()

    # 合并源名（sources.db 只读）
    articles: list[dict] = []
    sids = sorted({r["source_id"] for r in rows if r["source_id"]})
    srcmap: dict[int, str] = {}
    if sids:
        src_db = db_path.parent / "sources.db"
        if src_db.is_file():
            scon = sqlite3.connect(f"file:{src_db}?mode=ro", uri=True)
            try:
                for s in scon.execute(
                    f"SELECT id, name FROM sources WHERE id IN ({','.join('?' * len(sids))})", sids
                ):
                    srcmap[s[0]] = s[1]
            finally:
                scon.close()
    for r in rows:
        articles.append({
            "title": r["title"] or "?",
            "link": r["link"] or "",
            "author": r["author"] or "",
            "published": r["published"] or "",
            "summary": r["summary"] or "",
            "content": r["content"] or "",
            "source_id": r["source_id"],
            "source_name": srcmap.get(r["source_id"], "?"),
        })
    return articles


# ── 判类/提炼：便宜模型（分类 + 单条条目） ─────────────────────────────


_CLASSIFY_SYSTEM_PROMPT = """你是行业追踪的分类员。读下面每一条文章，判断：
1) 它是否属于 AI 领域（是/否）；
2) 归入哪个话题（模型与大模型 / Agent与工具 / 检索与知识库 / 评测 / 成本与部署 / 合规与监管 / 产品与商业 / 其他）；
3) 一句话摘要（≤50 字，中文）。

只输出 JSON，格式：{"items": [{"index": 0, "relevant": true, "topic": "...", "one_liner": "..."}]}
不相关（relevant=false）的只填 index 和 relevant。不要输出任何多余文字。"""


def classify_articles(llm: Callable, articles: list[dict], prompt_path: Path) -> list[dict]:
    """便宜模型判类：过滤出 AI 相关文章并归话题。分批调用，返回 article + 分类 的合并 dict 列表。"""
    if not articles:
        return []
    BATCH = 10
    out: list[dict] = []
    for start in range(0, len(articles), BATCH):
        batch = articles[start : start + BATCH]
        # 组装材料：只给标题+摘要+正文前 600 字，控制 token
        material_lines = []
        for i, a in enumerate(batch):
            body = (a["content"] or a["summary"] or "")[:600]
            material_lines.append(
                f"[{i}] 标题: {a['title']}\n来源: {a['source_name']}\n时间: {a['published'][:16]}\n"
                f"正文: {body}"
            )
        material = "\n\n".join(material_lines)
        user_prompt = f"以下是 {len(batch)} 篇文章，请逐条判断：\n\n{material}"
        try:
            result = llm(f"{_CLASSIFY_SYSTEM_PROMPT}\n\n{user_prompt}")
        except Exception as exc:
            raise RuntimeError(f"判类模型调用失败（第 {start // BATCH + 1} 批）：{exc}") from exc

        items = result.get("items", []) if isinstance(result, dict) else []
        by_index = {int(it.get("index")): it for it in items if isinstance(it, dict)}
        for i, a in enumerate(batch):
            it = by_index.get(i, {})
            a["relevant"] = bool(it.get("relevant"))
            a["topic"] = str(it.get("topic") or "其他")
            a["one_liner"] = str(it.get("one_liner") or "")
            out.append(a)
    return out


# ── 提炼条目：便宜模型（逐条走判断动作，产出知识库条目） ───────────────


def extract_entries(
    llm: Callable,
    relevant: list[dict],
    prompt_path: Path,
    gap_list_text: str,
    production_notes_text: str,
) -> list[dict]:
    """把相关文章按判断动作提炼成知识库条目（JSON 通道，字段写死）。

    提炼是机械字段填充：用精简判断要点（内联，不读 SOP 全文），
    避免每批都带 8KB 人设+反例撑爆上下文、拖慢生成。
    """
    if not relevant:
        return []
    gap_block = gap_list_text or "（我们还没整理缺口清单）"
    prod_block = production_notes_text or "（不知道，需要团队确认）"
    # 精简判断要点（来自 docs/S052-R13分析规则-判断动作与条目结构.md 第二、十节）
    judgement = (
        "对每条材料按顺序判断：\n"
        "1) 它解决什么问题、在什么场景、不解决会怎样。\n"
        "2) 三向对照：①生产在用的方案（只能来自我给的来源，没有就写\"不知道，需要团队确认\"）\n"
        "  ②社区原本流行的方案（上一代主流，当时解决什么、后来卡在哪；找不到前身就写\"没找到前身\"）\n"
        "  ③这个新东西改了什么（换掉哪个假设、新增什么依赖、挪走哪个瓶颈）。\n"
        "3) 创新属于哪一类：创意缺口（技术早能做只是没人组合）/能力突破（依赖新模型/新硬件才成立）/\n"
        "  成本突破（量级变化，便宜若干倍/快一个数量级）/约束变化（法规、生态、标准变了才成立）；"
        "至少给一条判据，判不了就写\"判不了\"。\n"
        "4) 拓开了什么边界：以前做不到或不划算的什么事现在能做了；什么条件下不成立。\n"
        "5) 能不能落地：五关（成本/合规与风险/运维/人手/迁移与回退）逐条说，缺哪几个条件列出来。\n"
        "6) 补的是我们哪条缺口：只能对照我给的缺口清单；清单里没有就写\"不补我们的缺口\"；"
        "清单为空写\"我们还没整理缺口清单\"，不许拿行业通用痛点冒充。\n"
        "7) 代价：多花了什么，谁因此不接受它。\n"
        "8) 对我们的意义：跟选型、产品方向的关系；跟不跟，第一步做什么。\n\n"
        "硬规则：只用手上给到的材料，不上网补料；数字/日期/原话必须能在原文里找到，"
        "找不到就不写精确值；作者自述的创新点标\"作者自述\"；互相矛盾的说法都列出来不许选边；"
        "材料只有转述没有可判断细节就写\"这条只有转述\"。\n"
    )
    BATCH = 5
    out: list[dict] = []
    for start in range(0, len(relevant), BATCH):
        batch = relevant[start : start + BATCH]
        material_lines = []
        for i, a in enumerate(batch):
            body = (a["content"] or a["summary"] or "")[:1200]
            material_lines.append(
                f"[{i}] 标题: {a['title']}\n来源: {a['source_name']} | 时间: {a['published'][:16]}\n"
                f"链接: {a['link']}\n正文: {body}"
            )
        user_prompt = (
            f"{judgement}\n"
            f"生产在用的方案（① 来源，没有就写不知道）：\n{prod_block}\n\n"
            f"缺口清单：\n{gap_block}\n\n"
            f"以下是 {len(batch)} 条材料，逐条按上述判断提炼成条目：\n\n"
            + "\n\n".join(material_lines)
            + "\n\n只输出 JSON：{\"entries\": [{\"index\": 0, \"title\": \"...\", "
            "\"one_liner\": \"...\", \"prod_usage\": \"...\", \"community_popular\": \"...\", "
            "\"what_changed\": \"...\", \"innovation_type\": \"...\", \"innovation_evidence\": \"...\", "
            "\"boundary\": \"...\", \"landing\": \"...\", \"gap\": \"...\", \"priority\": \"...\", "
            "\"significance\": \"...\", \"evidence_tier\": \"T1|T2|T3|T4|T5\", \"link\": \"...\", "
            "\"published\": \"...\"}]}"
            "每条 150–400 字；字段值里不要换行（用空格）；缺字段填空串。"
        )
        try:
            result = llm(f"{user_prompt}")
        except Exception as exc:
            raise RuntimeError(f"提炼条目模型调用失败（第 {start // BATCH + 1} 批）：{exc}") from exc
        entries = result.get("entries", []) if isinstance(result, dict) else []
        # 关联回原文
        by_index = {int(e.get("index")): e for e in entries if isinstance(e, dict)}
        for i, a in enumerate(batch):
            e = by_index.get(i, {})
            out.append({
            "title": str(e.get("title") or a["title"]),
            "one_liner": str(e.get("one_liner") or ""),
            "prod_usage": str(e.get("prod_usage") or ""),
            "community_popular": str(e.get("community_popular") or ""),
            "what_changed": str(e.get("what_changed") or ""),
            "innovation_type": str(e.get("innovation_type") or ""),
            "innovation_evidence": str(e.get("innovation_evidence") or ""),
            "boundary": str(e.get("boundary") or ""),
            "landing": str(e.get("landing") or ""),
            "gap": str(e.get("gap") or ""),
            "priority": str(e.get("priority") or ""),
            "significance": str(e.get("significance") or ""),
            "evidence_tier": str(e.get("evidence_tier") or ""),
            "link": str(e.get("link") or a["link"]),
            "published": a["published"],  # 原文发布时间为准，不信任模型截断值
            "source_name": a["source_name"],
            "topic": a["topic"],
        })
    return out


# ── 写每期总结：大模型（给人读的一篇） ─────────────────────────────────


def write_period_summary(
    llm: Callable,
    entries: list[dict],
    prompt_path: Path,
    previous_summary: str,
) -> str:
    """大模型写每期总结（给人读的一篇，2–4k 字，只挑最突出的几条）。"""
    if not entries:
        return ""
    sop = prompt_path.read_text(encoding="utf-8") if prompt_path.is_file() else ""
    entries_block = "\n\n".join(
        f"[{i}] 《{e['title']}》 ({e['source_name']}, {_fmt_date(e['published'])})\n"
        f"一句话: {e['one_liner']}\n"
        f"创新: {e['innovation_type']}（{e['innovation_evidence']}）\n"
        f"能不能落地: {e['landing']}\n"
        f"补哪条缺口: {e['gap']}\n"
        f"意义: {e['significance']}\n"
        f"链接: {e['link']}"
        for i, e in enumerate(entries)
    )
    prev_block = previous_summary[:3000] if previous_summary else "（这是第一期，没有上期）"
    user_prompt = (
        f"阅读 SOP（人设/判断动作/硬规则/反例/输出要求）：\n{sop}\n\n"
        f"上期总结（阶段结论对比用）：\n{prev_block}\n\n"
        f"本期提炼出的条目（{len(entries)} 条，从中挑选最突出的 3–6 条来写，不求覆盖全部）：\n\n"
        f"{entries_block}\n\n"
        "写一篇中文技术分析文档（给人读）：2–4k 字；先结论后依据；每个新东西都写代价与"
        "不成立条件；每条判断带原文链接；承担阶段结论（相对上期变了什么，新增哪些判断、"
        "推翻了哪条）；结尾不写总结段。"
    )
    try:
        # 大模型走文本通道（Markdown 全文即产物）
        return llm(user_prompt, as_text=True)
    except Exception as exc:
        raise RuntimeError(f"写总结模型调用失败：{exc}") from exc


# ── 落盘（知识结构已定） ──────────────────────────────────────────────


def _fmt_date(published: str, fallback: str = "") -> str:
    """把 RFC 822 发布时间转 YYYY-MM-DD（解析失败返回原样前 10 位）。"""
    if not published:
        return fallback
    try:
        dt = email.utils.parsedate_to_datetime(published)
        return dt.date().isoformat()
    except Exception:
        return published[:10]


def slugify(name: str) -> str:
    """文件名安全化：中文保留、非法字符替换为 _。"""
    cleaned = re.sub(r'[\\/:*?"<>|\s]+', "_", name)
    return cleaned.strip("_")[:60] or "untitled"


def ensure_structure(root: Path) -> None:
    """创建知识结构目录（幂等）。"""
    (root / "话题").mkdir(parents=True, exist_ok=True)
    (root / "每期总结").mkdir(parents=True, exist_ok=True)


def write_index(root: Path, entries: list[dict]) -> None:
    """重写索引.md：全部条目一行一条（话题 | 标题 | 来源 | 日期 | 状态 | 证据等级 | 链接）。"""
    lines = ["# 行业追踪索引\n", "| 话题 | 标题 | 来源 | 日期 | 状态 | 证据等级 | 链接 |", "|---|---|---|---|---|---|---|"]
    # 保留已有有效条目（状态=有效/已被替代/有争议），追加本期新条目
    existing = _read_existing_index(root)
    rows: dict[str, dict] = {}
    for e in existing:
        rows[e["link"]] = e
    for e in entries:
        rows[e["link"]] = {
            "topic": e["topic"], "title": e["title"], "source": e["source_name"],
            "published": _fmt_date(e["published"]), "status": "有效", "evidence_tier": e["evidence_tier"] or "—",
            "link": e["link"],
        }
    for row in rows.values():
        lines.append(
            f"| {row['topic']} | {row['title']} | {row['source']} | {row['published']} | "
            f"{row['status']} | {row['evidence_tier']} | {row['link']} |"
        )
    (root / "索引.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _read_existing_index(root: Path) -> list[dict]:
    """读回现有索引行（供保留旧条目；结构简单，逐行解析）。"""
    idx = root / "索引.md"
    out: list[dict] = []
    if not idx.is_file():
        return out
    for line in idx.read_text(encoding="utf-8").splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 7 or cells[0] == "话题" or set(cells[0]) <= set("-"):
            continue
        out.append({
            "topic": cells[0], "title": cells[1], "source": cells[2],
            "published": cells[3], "status": cells[4], "evidence_tier": cells[5], "link": cells[6],
        })
    return out


def write_topic_entry(root: Path, entry: dict) -> Path:
    """写单条条目文件：话题/<话题>/<按概念命名>.md。返回写入路径。"""
    topic_dir = root / "话题" / entry["topic"]
    topic_dir.mkdir(parents=True, exist_ok=True)
    # 按概念命名：标题去掉标点；若已存在同概念文件，追加编号
    fname = slugify(entry["title"]) + ".md"
    path = topic_dir / fname
    n = 2
    while path.is_file() and path.read_text(encoding="utf-8").find(entry["link"]) == -1:
        path = topic_dir / f"{slugify(entry['title'])}_{n}.md"
        n += 1
    md = (
        f"### {entry['title']}（[原文]({entry['link']}) · {_fmt_date(entry['published'])} · {entry['source_name']}）\n\n"
        f"**一句话**：{entry['one_liner']}\n\n"
        f"**① 生产在用的方案**：{entry['prod_usage']}\n\n"
        f"**② 社区原本的流行方案**：{entry['community_popular']}\n\n"
        f"**③ 这个新东西改了什么**：{entry['what_changed']}\n\n"
        f"**创新属于哪一类**：{entry['innovation_type']}（{entry['innovation_evidence']}）\n\n"
        f"**拓开的边界**：{entry['boundary']}\n\n"
        f"**能不能落地**：{entry['landing']}\n\n"
        f"**补的是我们哪条缺口**：{entry['gap']}\n\n"
        f"**优先级判定**：{entry['priority']}\n\n"
        f"**对我们的意义**：{entry['significance']}\n\n"
        f"**证据等级**：{entry['evidence_tier'] or '—'}\n\n"
        f"**原文链接**：{entry['link']}\n"
    )
    path.write_text(md, encoding="utf-8")
    return path


def append_update_log(root: Path, line: str) -> None:
    """更新日志只追加：每期一行（新增几条/被替代几条/待复核几条）。"""
    log = root / "更新日志.md"
    if not log.is_file():
        log.write_text("# 更新日志\n\n", encoding="utf-8")
    with log.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def save_period_summary(root: Path, date_str: str, seq: int, md_text: str) -> Path:
    """落每期总结：每期总结/<日期>-第NN期.md。返回路径。"""
    out = root / "每期总结" / f"{date_str}-第{seq:02d}期.md"
    out.write_text(md_text, encoding="utf-8")
    return out


def next_period_seq(root: Path) -> int:
    """下一期编号：按现有 每期总结/*.md 文件名取最大期号 +1。"""
    d = root / "每期总结"
    if not d.is_dir():
        return 1
    max_seq = 0
    for f in d.glob("*.md"):
        m = re.search(r"第(\d+)期", f.name)
        if m:
            max_seq = max(max_seq, int(m.group(1)))
    return max_seq + 1


# ── 主流程 ─────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="industry_digest.py",
        description="R13 行业追踪：读信源库新文章 → 提炼条目进知识库 → 写每期总结",
    )
    ap.add_argument("--days", type=int, default=None, help=f"近 N 天（默认 {DEFAULT_DAYS}）")
    ap.add_argument("--provider-classify", type=str, default=None, help="判类/提炼用 provider（便宜模型）")
    ap.add_argument("--provider-write", type=str, default=None, help="写总结用 provider（大模型）")
    ap.add_argument("--output", type=str, default=None, help="产物根目录（默认 output/行业追踪）")
    ap.add_argument("--dry-run", action="store_true", help="只列出候选文章，不调模型不落盘")
    ap.add_argument("--source-db", type=str, default=None, help="信源库 articles.db 路径")
    ap.add_argument("--max-articles", type=int, default=None, help="最多取多少篇候选（诊断/小规模验证用）")
    args = ap.parse_args(argv)

    try:
        dc = load_digest_config()
    except Exception as exc:
        print(f"ERROR: 读取配置失败：{exc}", file=sys.stderr)
        return 1
    if args.days:
        dc["days"] = args.days
    if args.provider_classify:
        dc["provider_classify"] = args.provider_classify
    if args.provider_write:
        dc["provider_write"] = args.provider_write
    if args.output:
        dc["output_root"] = Path(args.output)
    if args.source_db:
        dc["source_db"] = Path(args.source_db)

    # 0. 读取信源库
    try:
        articles = fetch_recent_articles(dc["source_db"], dc["days"])
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    if args.max_articles:
        articles = articles[: args.max_articles]
    if not articles:
        print(f"INFO: 近 {dc['days']} 天没有新文章（或信源库为空），本期不生成总结。")
        return 0
    print(f"INFO: 信源库近 {dc['days']} 天共 {len(articles)} 篇候选。")

    if args.dry_run:
        for a in articles:
            print(f"  - [{_fmt_date(a['published'])}] {a['title']} ({a['source_name']})")
        return 0

    # 1. 判类（便宜模型）
    try:
        llm_classify = build_llm(load_config(), provider=dc["provider_classify"])
    except Exception as exc:
        print(f"ERROR: 判类模型未配置：{exc}\n请在 config.yaml 的 llm.providers 配置 {dc['provider_classify']}，"
              f"或在 config.yaml 的 industry_digest 段指定 provider。", file=sys.stderr)
        return 1
    print(f"INFO: 判类/提炼模型 {dc['provider_classify']} 调用中（{len(articles)} 篇）…")
    try:
        classified = classify_articles(llm_classify, articles, dc["prompt_path"])
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    relevant = [a for a in classified if a.get("relevant")]
    print(f"INFO: 判类完成，AI 相关 {len(relevant)}/{len(articles)} 篇。")

    # 2. 提炼条目（便宜模型）
    gap_text = ""
    gap_file = dc["output_root"] / "缺口清单.md"
    if gap_file.is_file():
        gap_text = gap_file.read_text(encoding="utf-8")
    # 生产在用的方案：团队维护清单（暂定默认：output/行业追踪/生产在用方案.md，没有就写不知道）
    prod_file = dc["output_root"] / "生产在用方案.md"
    prod_text = prod_file.read_text(encoding="utf-8") if prod_file.is_file() else ""
    if not relevant:
        print("INFO: 没有 AI 相关文章，本期不生成总结。")
        return 0
    try:
        entries = extract_entries(llm_classify, relevant, dc["prompt_path"], gap_text, prod_text)
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"INFO: 提炼完成，{len(entries)} 条条目。")

    # 3. 落盘（知识结构）
    root = dc["output_root"]
    ensure_structure(root)
    written: list[Path] = []
    for e in entries:
        try:
            p = write_topic_entry(root, e)
            written.append(p)
        except Exception as exc:  # 单条失败不中断
            print(f"WARN: 条目「{e.get('title')}」落盘失败：{exc}")
    write_index(root, entries)
    date_str = datetime.date.today().isoformat()
    seq = next_period_seq(root)
    append_update_log(root, f"- {date_str} 第{seq:02d}期：新增 {len(entries)} 条条目，替代 0 条，待复核 0 条。")

    # 4. 质量检查（只提示不阻断）
    try:
        from components.guards.digest_quality import audit_digest_quality
        qc_report = {"errors": [], "warnings": [], "notes": []}
        for p in written:
            e = next((x for x in entries if x["link"] in p.read_text(encoding="utf-8")), None)
            if e:
                try:
                    from components.guards.digest_quality import audit_entry_fields
                    r = audit_entry_fields(e)
                    qc_report["warnings"].extend(r["warnings"])
                except Exception as exc:
                    qc_report["notes"].append(f"条目检查异常：{exc}")
    except ImportError:
        qc_report = {"errors": [], "warnings": [], "notes": ["digest_quality 未找到，跳过质量检查"]}

    # 5. 写每期总结（大模型）
    if entries:
        prev = ""
        prev_dir = root / "每期总结"
        prevs = sorted(prev_dir.glob("*.md")) if prev_dir.is_dir() else []
        if prevs:
            prev = prevs[-1].read_text(encoding="utf-8")
        try:
            llm_write = build_llm(load_config(), provider=dc["provider_write"])
        except Exception as exc:
            print(f"ERROR: 写总结模型未配置：{exc}", file=sys.stderr)
            return 1
        print(f"INFO: 写总结模型 {dc['provider_write']} 调用中（{len(entries)} 条条目）…")
        try:
            summary_md = write_period_summary(llm_write, entries, dc["prompt_path"], prev)
        except RuntimeError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        if summary_md.strip():
            s_path = save_period_summary(root, date_str, seq, summary_md)
            print(f"INFO: 每期总结已落盘：{s_path}")
            # 总结质量检查
            try:
                qc_summary = audit_digest_quality(summary_md)
                qc_report["warnings"].extend(qc_summary["warnings"])
                qc_report["notes"].extend(qc_summary["notes"])
            except Exception as exc:
                qc_report["notes"].append(f"总结检查异常：{exc}")
        else:
            print("WARN: 每期总结为空，未落盘。")

    # 汇总
    print(f"DONE: 知识库条目 {len(written)} 条，索引与日志已更新，产物在 {root}")
    if qc_report["warnings"]:
        print(f"WARN: 质量检查 {len(qc_report['warnings'])} 条提示（不阻断）：")
        for w in qc_report["warnings"][:15]:
            print(f"  - {w}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
