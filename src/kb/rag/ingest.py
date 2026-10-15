# RAG 子库 - 入库器（概念层 Markdown + 论文层 JSON → 分块 → 嵌入 → ChromaDB）
"""RAGIngest：把概念层文档与论文层数据写进 ChromaDB 的 agent_knowledge collection，
并提供增量单文档的幂等入库入口 ingest_document（格式契约 + 先删后插 + 孤儿清理 + hash 跳过）。

设计要点（对应《RAG知识库实施规划》4.3 / 5.3 / §六，选型笔记「复审四」）：
- 单个 collection：agent_knowledge，余弦距离（metadata {"hnsw:space": "cosine"}）；
- 概念层：遍历目录下一级子目录（跳过 assets、点开头目录、非目录），
  每个目录 rglob("*.md") 逐篇处理；section-aware 分块由 splitter 负责；
- 论文层：整篇一块（"# 标题 + abstract"），abstract 缺失或去空白后短于 50 字符的跳过；
- 幂等：每篇文档入库前先按 metadata.source 删除旧 chunk（先删后插，重跑不翻倍）；
- 增量单文档（ingest_document）：先删后插 → 确定性 chunk ID
  「doc_id:strategy_version:chunk_index」→ 孤儿清理 → 源级状态表（content_hash +
  strategy_version 均未变则整条流水线跳过）；
- metadata 值只放 str/int/float/bool（None 会让 ChromaDB 报错），
  本期没有飞书对应关系，因此不写 feishu_ref 键；
- 存量两条路径（ingest_concept_dir / ingest_papers）与增量路径（ingest_document）每批入库后
  都挂「重建词法索引」钩子（rebuild_lexical_index）：词法索引是全库纯函数、毫秒级全量重建，
  失败只打日志不阻断入库（检索侧按库内容在内存重建，见 store.py / lexical.py）；
- 嵌入通过 embedder 注入：测试传假 embedder，零真实 API。
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import sys
import warnings
from pathlib import Path

import chromadb
import yaml

from kb.rag.embeddings import EmbeddingModel
from kb.rag.lexical import LexicalIndex, default_lexical_dir, default_userdict_path
from kb.rag.splitter import split_document


# ChromaDB collection 名（三层共用一个 collection，靠 metadata.layer 区分）
COLLECTION_NAME = "agent_knowledge"
# 分块策略版本号，写进每个 chunk 的 metadata，便于日后整库重建比对
# v3：断点优先级（标题 > 段落 > 约束词 > 字数上限）+ Contextual Header 前缀 + 父子块
# v4：约束词表补英文对照（整词匹配、大小写不敏感），句末标点集纳入半角 "."——
#     条件句保护对英文语料首次真正生效，故旧 v3 块全库重建
CHUNKING_VERSION = "v4"
# 论文 abstract 去空白后的最小有效长度（短于此视为不可用，跳过）
MIN_ABSTRACT_CHARS = 50
# 概念层目录跳过名单（图片等资源目录）
SKIP_DIR_NAMES = {"assets"}
# 概念层目录名未命中映射表时的兜底分类
FALLBACK_CONCEPT_CATEGORY = "architecture"
# 论文 category 未命中映射表时的兜底分类
FALLBACK_PAPER_CATEGORY = "applications-other"
# 论文层前缀：layer 用法名 → chunk ID 前缀
LAYER_ID_PREFIX = {"curated-paper": "curated", "tracked-paper": "tracked"}

# ── 增量单文档入库（ingest_document）相关常量 ────────────────────────
# 切片策略版本号：写进 chunk ID 中段与源级状态表；变更即意味着全库重建
# （选型笔记复审四第 5 条），因此它是「是否跳过」的两个判据之一。
STRATEGY_VERSION = "v2"
# 源级状态表（content_hash / strategy_version / chunk id 集合）缺省落盘位置
DEFAULT_STATE_PATH = "./domain_kb/state"
STATE_FILE_NAME = "ingest_state.json"
# 增量文档契约 type → splitter 切片策略名。
# 本任务不改 splitter 的切片策略，而 splitter 现行只认两种策略：
# "concept"（按 1-3 级标题结构切片）与 "paper"（整篇一块）。
# 契约的 note/doc/snippet 都是带标题的普通文档，一律映射到结构切片 "concept"；
# 其余 type（如 paper / interpretation）原样透传，由 splitter 自己兜底。
INCREMENT_SLICER_TYPES = {"note": "concept", "doc": "concept", "snippet": "concept"}
# 增量文档写在 metadata.layer 上的取值：`concept` 这个旧桶。
# 规划 §4.3 的 v2 metadata schema 已不再定义 layer 字段（增量身份靠 domain=increment
# + doc_type 表达、质量靠 quality，选型笔记复审四亦写明 quality 取代 v1 layer 权重），
# 但 store.py 是冻结件、只认得 concept / curated-paper / tracked-paper 三个层值：
# 自造第四个层值会让「按层过滤 / 分层加权」的既有调用点把增量文档整批漏掉。
# 故 layer 沿用最贴近的旧桶 concept，增量身份由 domain / quality / doc_type 承载。
INCREMENT_LAYER = "concept"
# 增量文档质量档（统一 metadata schema 4.3）：用户手输/投递的文档
INCREMENT_QUALITY = "user_input"
# 增量文档缺省 chunk_type（本期只有整篇结构切片一种）
INCREMENT_CHUNK_TYPE = "section"


# 概念层目录名 → 统一分类（31 项，硬编码；未命中走 architecture 兜底）
CONCEPT_CATEGORY_MAP = {
    "Concepts": "concepts",
    "Introduction": "concepts",
    "DesignPatterns": "architecture",
    "Architecture": "architecture",
    "ReferenceArchitecture": "architecture",
    "AgenticFrameworks": "frameworks",
    "WorkflowBuilders": "frameworks",
    "AgentPlatforms": "platforms",
    "Marketplace": "platforms",
    "Standards": "standards",
    "AgentHarness": "harness",
    "AgenticTechStack": "harness",
    "AgentMemory": "memory",
    "EvaluationFrameworks": "evaluation",
    "Benchmarks": "benchmarks",
    "SecurityFrameworks": "safety",
    "AIGovernance": "safety",
    "Observability": "observability",
    "AgentOps": "observability",
    "ProductionBestPractices": "production",
    "MaturityModels": "maturity-models",
    "ContextEngineering": "context-engineering",
    "PromptEngineering": "prompt-engineering",
    "RAG": "rag",
    "AICodingAgents": "coding",
    "Wizard": "applications-other",
    "AllThingsAnthropic": "vendor-anthropic",
    "AllThingsOpenAI": "vendor-openai",
    "AllThingsGoogle": "vendor-google",
    "AllThingsMicrosoft": "vendor-microsoft",
    "AllThingsAWS": "vendor-aws",
}

# 论文原 category → 统一分类（10 项）
PAPER_CATEGORY_MAP = {
    "surveys": "surveys",
    "architectures": "architecture",
    "planning": "planning",
    "memory": "memory",
    "tool-use": "tool-use",
    "multi-agent": "multi-agent",
    "environments": "environments",
    "applications": "applications-other",
    "evaluation": "evaluation",
    "safety": "safety",
}


def _extract_front_matter(text: str) -> dict:
    """读文首 YAML front matter；无 front matter、未闭合或解析失败时返回空 dict。"""
    lines = str(text).splitlines()
    if not lines or lines[0].lstrip("\ufeff").strip() != "---":
        return {}
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            raw = "\n".join(lines[1:index])
            try:
                data = yaml.safe_load(raw)
            except yaml.YAMLError:
                return {}
            return data if isinstance(data, dict) else {}
    return {}


def _paper_skip_reason(paper: dict) -> str:
    """论文条目被跳过的原因："abstract" 表示 summary 过短/缺失，"arxiv_id" 表示缺 ID，"" 表示可入库。"""
    paper = paper or {}
    if len(str(paper.get("abstract") or "").strip()) < MIN_ABSTRACT_CHARS:
        return "abstract"
    if not str(paper.get("arxiv_id") or "").strip():
        return "arxiv_id"
    return ""


def _is_curated(value) -> bool:
    """论文 is_starter_kit 归一化为布尔。

    兼容两种数据形态：字符串 "True"/"False"（任务书口径）与真布尔 True/False
    （当前 papers_with_abstract.json 实际形态），非布尔值按字符串比较。
    """
    if isinstance(value, bool):
        return value
    return str(value).strip() == "True"


# ── 增量：确定性 ID ──────────────────────────────────────────────────


def doc_id_for_source(source: str) -> str:
    """doc_id 由 source 派生（稳定、与运行目录无关）：md5(source) 前 12 位。"""
    return hashlib.md5(str(source).encode("utf-8")).hexdigest()[:12]


def chunk_id_for(doc_id: str, strategy_version: str, chunk_index: int) -> str:
    """确定性 chunk ID：三段用冒号拼接「doc_id:strategy_version:chunk_index」。

    邻居变化该块保留原 ID，写入是覆盖不是追加 → 重跑幂等、不产生重复
    （规划 4.3 / 选型笔记复审四第 2 条）。
    """
    return f"{doc_id}:{strategy_version}:{chunk_index}"


def _join_tags(value) -> str:
    """metadata 里的 tags：Chroma 不收 list，统一拼成逗号分隔字符串。"""
    if value is None:
        return ""
    if isinstance(value, (list, tuple, set)):
        return ",".join(str(item).strip() for item in value if str(item).strip())
    return str(value).strip()


def _clean_meta(meta: dict) -> dict:
    """metadata 归一化：只留 Chroma 接受的 str/int/float/bool，None 键直接丢掉。"""
    cleaned: dict = {}
    for key, value in (meta or {}).items():
        if value is None:
            continue
        if isinstance(value, (bool, int, float, str)):
            cleaned[key] = value
        elif isinstance(value, (list, tuple, set)):
            cleaned[key] = _join_tags(value)
        else:
            cleaned[key] = str(value)
    return cleaned


class IngestState:
    """源级状态表：source → {content_hash, strategy_version, chunk_ids, ...}。

    落盘为 <state_path>/ingest_state.json（规划 4.2 / §八 state_path）。本项目
    规模下单个 JSON 文件够用且零新依赖；写盘用「临时文件 + os.replace」，
    避免中途失败留下半个 JSON。
    """

    VERSION = 1

    def __init__(self, state_dir):
        self.dir = Path(str(state_dir or DEFAULT_STATE_PATH))
        self.path = self.dir / STATE_FILE_NAME
        self._sources: dict = self._load()

    def _load(self) -> dict:
        """读状态表；文件缺失返回空表，损坏则告警后按空表处理（不阻断入库）。"""
        if not self.path.is_file():
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            warnings.warn(
                f"状态表读取失败，按空表处理（将重新入库）：{self.path}（{exc}）",
                UserWarning,
                stacklevel=2,
            )
            return {}
        sources = payload.get("sources") if isinstance(payload, dict) else None
        return sources if isinstance(sources, dict) else {}

    def get(self, source: str) -> dict:
        """取某个 source 的状态条目（无记录返回 {}）。"""
        entry = self._sources.get(str(source))
        return dict(entry) if isinstance(entry, dict) else {}

    def is_unchanged(self, source: str, content_hash: str, strategy_version: str) -> bool:
        """content_hash 与 strategy_version 均未变 → True（整条流水线跳过）。"""
        entry = self.get(source)
        if not entry:
            return False
        return (
            str(entry.get("content_hash") or "") == str(content_hash)
            and str(entry.get("strategy_version") or "") == str(strategy_version)
        )

    def chunk_ids(self, source: str) -> list[str]:
        """该 source 上次入库的 chunk id 列表（孤儿清理的旧集合来源之一）。"""
        return [str(item) for item in (self.get(source).get("chunk_ids") or [])]

    def record(self, source: str, **fields) -> None:
        """写入/更新某个 source 的状态条目（None 值不覆盖已有字段）。"""
        entry = self._sources.setdefault(str(source), {})
        entry.update({key: value for key, value in fields.items() if value is not None})
        entry["ingested_at"] = datetime.datetime.now().isoformat(timespec="seconds")

    def save(self) -> None:
        """原子落盘（写临时文件后 os.replace）。"""
        self.dir.mkdir(parents=True, exist_ok=True)
        payload = {"version": self.VERSION, "sources": self._sources}
        tmp_path = self.path.with_name(self.path.name + ".tmp")
        tmp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(tmp_path, self.path)

    def sources(self) -> list[str]:
        """状态表里已登记的 source 列表（排序，便于核对）。"""
        return sorted(self._sources)


class RAGIngest:
    """领域知识库入库器（概念层 Markdown / 论文层 JSON / 增量单文档）。

    Args:
        config: config.yaml 根 dict，内部取 config["domain_kb"]。
        embedder: 可选嵌入器；传入即用（测试注入假 embedder），未传则用
            EmbeddingModel(domain_kb["embedding"])（真实调用硅基流动 API）。
    """

    def __init__(self, config: dict, embedder=None):
        domain_kb = dict((config or {}).get("domain_kb") or {})
        lexical_cfg = dict(domain_kb.get("lexical") or {})
        self.domain_kb = domain_kb
        # 相对路径以 cwd（项目根）解析：Path 保持相对形态，交给 ChromaDB 解析
        self.chroma_path = Path(str(domain_kb.get("chroma_path") or "./domain_kb/chroma"))
        # 增量入库的切片策略版本与源级状态表位置（config 未给则用缺省值）
        self.strategy_version = str(domain_kb.get("strategy_version") or STRATEGY_VERSION)
        self.state_path = Path(str(domain_kb.get("state_path") or DEFAULT_STATE_PATH))
        # 词法索引位置与 jieba 自定义词典（缺省落在 Chroma 同级目录，与 store.py 一致）
        self.lexical_path = Path(
            str(
                domain_kb.get("lexical_path")
                or lexical_cfg.get("path")
                or default_lexical_dir(self.chroma_path)
            )
        )
        self.userdict_path = Path(
            str(
                lexical_cfg.get("user_dict")
                or lexical_cfg.get("userdict")
                or default_userdict_path(self.chroma_path)
            )
        )
        self.lexical_enabled = True if lexical_cfg.get("enabled") is None else bool(lexical_cfg.get("enabled"))
        self._state: IngestState | None = None
        self.client = chromadb.PersistentClient(path=str(self.chroma_path))
        self.collection = self.client.get_or_create_collection(
            COLLECTION_NAME, metadata={"hnsw:space": "cosine"}
        )
        if embedder is not None:
            self.embedder = embedder
        else:
            self.embedder = EmbeddingModel(domain_kb.get("embedding") or {})

    @property
    def state(self) -> IngestState:
        """源级状态表（懒加载：只有增量入库才会读/写 domain_kb/state/）。"""
        if self._state is None:
            self._state = IngestState(self.state_path)
        return self._state

    # ── 词法索引：每批入库后的重建钩子 ─────────────────────────────

    def rebuild_lexical_index(self) -> bool:
        """重建全库词法索引（《RAG知识库实施规划》4.4：每批入库后整体重建）。

        词法索引是全库 chunk 的纯函数、本规模毫秒级，因此不做增量维护，入库后整库重建；
        重建后顺手落盘到 lexical_path（bm25s 索引文件 + chunk 记录），检索侧默认按库
        在内存重建（见 store.py），落盘件供核对与后续快路径使用。

        失败只打日志、返回 False，绝不向上抛：词法索引滞后于库不该让入库失败。
        """
        if not self.lexical_enabled:
            return False
        try:
            index = LexicalIndex.from_collection(
                self.collection, userdict_path=self.userdict_path
            )
            index.save(self.lexical_path)
        except Exception as exc:
            print(f"[rag_ingest] 词法索引重建失败（不影响入库）：{exc}", file=sys.stderr)
            return False
        print(f"[rag_ingest] 词法索引已重建：{len(index)} 条 → {self.lexical_path}")
        return True


    # ── 概念层 ────────────────────────────────────────────────────

    def ingest_concept_dir(self, dir_path: str, progress_every: int = 0) -> int:
        """批量导入概念层目录，返回写入的 chunk 数。

        只处理 dir_path 的一级子目录：跳过 assets、点开头目录、非目录项；
        根目录零散文件不处理。目录名未命中映射表时按 architecture 兜底并告警。

        Args:
            progress_every: 每处理满该数量个文件向 stderr 打一行进度；
                0（默认）表示不打进度，行为与不传参时完全一致。
        """
        root = Path(dir_path)
        if not root.is_dir():
            warnings.warn(f"概念层路径不存在或不是目录：{root}", UserWarning, stacklevel=2)
            return 0

        source_root = root.parent
        total = 0
        file_count = 0
        for sub_dir in sorted(root.iterdir()):
            if not sub_dir.is_dir():
                continue
            if sub_dir.name.startswith(".") or sub_dir.name in SKIP_DIR_NAMES:
                continue
            category = CONCEPT_CATEGORY_MAP.get(sub_dir.name)
            if category is None:
                warnings.warn(
                    f"概念层目录名 {sub_dir.name!r} 未命中分类映射表，按 "
                    f"{FALLBACK_CONCEPT_CATEGORY} 兜底入库",
                    UserWarning,
                    stacklevel=2,
                )
                category = FALLBACK_CONCEPT_CATEGORY
            for md_file in sorted(sub_dir.rglob("*.md")):
                if md_file.name.startswith("."):
                    continue
                total += self._ingest_concept_file(md_file, source_root, sub_dir.name, category)
                file_count += 1
                if progress_every > 0 and file_count % progress_every == 0:
                    print(f"[已处理 {file_count} 个文件，累计 {total} chunk]", file=sys.stderr)
        # 每批入库后重建词法索引（失败只打日志，不阻断入库）
        self.rebuild_lexical_index()
        return total

    def _ingest_concept_file(self, md_file: Path, source_root: Path, dir_name: str, category: str) -> int:
        """处理单篇概念文档：先删后插，返回写入 chunk 数。"""
        # source 用相对 source 根的路径字符串（形如 concept/Concepts/xxx.md），
        # 与平台无关、与运行目录无关，保证重跑稳定可复现
        try:
            source = md_file.relative_to(source_root).as_posix()
        except ValueError:
            source = md_file.as_posix()
        try:
            text = md_file.read_text(encoding="utf-8")
        except OSError as exc:
            warnings.warn(f"概念文档读取失败，跳过：{md_file}（{exc}）", UserWarning, stacklevel=2)
            return 0

        front_matter = _extract_front_matter(text)
        title = str(front_matter.get("title") or "").strip() or md_file.stem

        # 幂等：同一 source 先删旧 chunk，再插新 chunk
        self.delete_by_source(source)

        base_meta = {
            "layer": "concept",
            "category": category,
            "source": source,
            "title": title,
            "curated": True,
            "chunking_version": CHUNKING_VERSION,
            "source_category": dir_name,
        }
        chunks = split_document(text, "concept", base_meta)
        if not chunks:
            return 0

        source_hash = hashlib.md5(source.encode("utf-8")).hexdigest()[:12]
        ids = [f"concept-{source_hash}-{chunk['metadata']['chunk_index']}" for chunk in chunks]
        self._add_chunks(ids, chunks)
        return len(chunks)

    # ── 论文层 ────────────────────────────────────────────────────

    def ingest_papers(
        self, papers: list[dict], layer: str = "curated-paper", progress_every: int = 0
    ) -> int:
        """批量导入论文 list[dict]，返回写入 chunk 数。

        layer 仅支持 curated-paper（精选层）与 tracked-paper（追踪层）。
        abstract 缺失/去空白后短于 50 字符的条目跳过；缺 arxiv_id 的条目跳过并告警。

        Args:
            progress_every: 每处理满该数量条目向 stderr 打一行进度（含跳过计数）；
                0（默认）表示不打进度，行为与不传参时完全一致。
        """
        if layer not in LAYER_ID_PREFIX:
            raise ValueError(
                f"不支持的论文层 {layer!r}，仅支持 {'/'.join(LAYER_ID_PREFIX)}"
            )
        prefix = LAYER_ID_PREFIX[layer]
        paper_list = list(papers or [])
        total = 0
        skipped = 0
        processed = 0
        for paper in paper_list:
            if _paper_skip_reason(paper):
                skipped += 1
            total += self._ingest_paper(paper, layer, prefix)
            processed += 1
            if progress_every > 0 and processed % progress_every == 0:
                print(
                    f"[已处理 {processed}/{len(paper_list)} 条，入库 {total} chunk，跳过 {skipped} 条]",
                    file=sys.stderr,
                )
        # 每批入库后重建词法索引（失败只打日志，不阻断入库）
        self.rebuild_lexical_index()
        # 入库进度参数
        return total

    def _ingest_paper(self, paper: dict, layer: str, prefix: str) -> int:
        """处理单篇论文：先删后插，返回写入 chunk 数（整篇一块，恒 1）。"""
        paper = paper or {}
        reason = _paper_skip_reason(paper)
        if reason == "arxiv_id":
            warnings.warn("论文明细缺少 arxiv_id，跳过该条", UserWarning, stacklevel=2)
        if reason:
            return 0

        abstract = str(paper.get("abstract") or "").strip()
        arxiv_id = str(paper.get("arxiv_id") or "").strip()

        raw_category = str(paper.get("category") or "").strip()
        category = PAPER_CATEGORY_MAP.get(raw_category, FALLBACK_PAPER_CATEGORY)
        title = str(paper.get("title") or "").strip()

        # 幂等：论文以 arxiv_id 为 source
        self.delete_by_source(arxiv_id)

        base_meta = {
            "layer": layer,
            "category": category,
            "source": arxiv_id,
            "title": title,
            "curated": _is_curated(paper.get("is_starter_kit")),
            "chunking_version": CHUNKING_VERSION,
            "source_category": raw_category,
            "arxiv_id": arxiv_id,
        }
        text = f"# {title}\n\n{abstract}"
        chunks = split_document(text, "paper", base_meta)
        if not chunks:
            return 0

        if len(chunks) == 1:
            ids = [f"{prefix}-{arxiv_id}"]
        else:
            # 防御性分支：论文层设计上恒为整篇一块，若未来分块策略变化也不撞 ID
            ids = [f"{prefix}-{arxiv_id}-{chunk['metadata']['chunk_index']}" for chunk in chunks]
        self._add_chunks(ids, chunks)
        return len(chunks)

    # ── 增量：单文档幂等入库 ───────────────────────────────────────

    def ingest_document(self, text: str, doc_type: str, source: str, **meta) -> dict:
        """单文档幂等入库（增量实时入口；规划 5.3 + 选型笔记复审四）。

        流程：
        1. 全文 content_hash（sha256）与状态表里该 source 的 content_hash、
           strategy_version 比对，两者都没变 → 整条流水线跳过（不切片、不嵌入、不动库）；
        2. 先按 source 删除旧 chunk（先删后插，重跑不翻倍）；
        3. 切片（契约 type → splitter 策略，见 INCREMENT_SLICER_TYPES）→ 批量嵌入 → upsert；
           chunk ID 取确定性形态「doc_id:strategy_version:chunk_index」，doc_id 由 source 派生；
        4. 孤儿清理：旧 chunk id 集合（状态表记录 ∪ 库里该 source 现存）减去新集合，
           删除差集（新版块数少于旧版时清掉残留）；
        5. 写状态表（content_hash / strategy_version / chunk_ids）；
        6. 打印一行摘要并返回统计 dict。

        Args:
            text: 文档全文（含 frontmatter，由格式契约解析后原样透传）。
            doc_type: 文档类型（note / doc / snippet / paper …）。
            source: 全局唯一来源键（幂等锚点），必填非空。
            **meta: 其余元数据（title / updated / tags / auto_wrapped / category /
                domain / quality / layer 等），未给的用缺省值补。

        Returns:
            {"source","doc_id","doc_type","content_hash","strategy_version","chunks",
             "written","source_chunks","previous_chunks","removed","orphans","skipped"}
            （chunks = 本次实际写入的 chunk 数，跳过时为 0；written 与之同值，保留给
              旧调用点；source_chunks = 该 source 入库后在库里的 chunk 总数；
              previous_chunks = 入库前该 source 的 chunk 数）
        """
        raw = str(text if text is not None else "")
        source_key = str(source or "").strip()
        if not source_key:
            raise ValueError("ingest_document 需要非空 source：source 是幂等锚点")
        doc_type_value = str(doc_type or "").strip() or "note"

        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        doc_id = doc_id_for_source(source_key)
        state = self.state
        previous = state.get(source_key)

        # 1. 幂等跳过：content_hash 与 strategy_version 均未变
        if state.is_unchanged(source_key, digest, self.strategy_version):
            existing = int(
                previous.get("chunks") or len(previous.get("chunk_ids") or [])
            )
            stats = {
                "source": source_key,
                "doc_id": doc_id,
                "doc_type": doc_type_value,
                "content_hash": digest,
                "strategy_version": self.strategy_version,
                # chunks / written = 本次真正写入的条数（跳过恒为 0，摘要与 inbox 汇总
                # 都按这个口径累加）；source_chunks / previous_chunks = 该 source
                # 入库前后在库里的 chunk 总数（跳过即沿用上次入库结果，库内条数不变）
                "chunks": 0,
                "written": 0,
                "source_chunks": existing,
                "previous_chunks": existing,
                "removed": 0,
                "orphans": 0,
                "skipped": True,
            }
            self._log_document_summary(stats)
            return stats

        base_meta = self._increment_meta(source_key, doc_id, doc_type_value, digest, meta)
        slicer_type = INCREMENT_SLICER_TYPES.get(doc_type_value, doc_type_value)

        # 旧 id 集合取两处：状态表记录 + 库里该 source 现存 chunk
        # （状态表缺失、或有人绕过管道直接写过库时，仍能清干净）
        old_ids = set(state.chunk_ids(source_key))
        old_ids.update(self._ids_by_source(source_key))

        # 2. 先删后插：只动被改的源
        removed = self.delete_by_source(source_key)

        # 3. 切片 + 确定性 ID + upsert
        chunks = split_document(raw, slicer_type, base_meta)
        ids = [
            chunk_id_for(doc_id, self.strategy_version, chunk["metadata"]["chunk_index"])
            for chunk in chunks
        ]
        if chunks:
            self._upsert_chunks(ids, chunks)

        # 4. 孤儿清理：旧 id 集合（状态表记录 ∪ 库里现存）− 新 id 集合，
        #    仍留在库里的（残留）删掉。先删后插已清掉本源旧块，这一步是兜底：
        #    状态表里有记录、库里还挂着 → 说明前一步没清干净，这里补刀。
        residue = set(self._ids_by_source(source_key))
        orphans = sorted((old_ids - set(ids)) & residue)
        if orphans:
            self.collection.delete(ids=orphans)

        # 5. 写状态表（源级：下次同 source 同 hash 直接跳过）
        state.record(
            source_key,
            doc_id=doc_id,
            content_hash=digest,
            strategy_version=self.strategy_version,
            chunk_ids=ids,
            chunks=len(ids),
            doc_type=doc_type_value,
            title=base_meta.get("title"),
            updated=base_meta.get("updated"),
        )
        state.save()

        stats = {
            "source": source_key,
            "doc_id": doc_id,
            "doc_type": doc_type_value,
            "content_hash": digest,
            "strategy_version": self.strategy_version,
            "chunks": len(ids),
            "written": len(ids),
            "source_chunks": len(ids),
            "previous_chunks": len(old_ids),
            "removed": removed,
            "orphans": len(orphans),
            "skipped": False,
        }
        # 6. 入库摘要
        self._log_document_summary(stats)
        # 7. 每批入库后重建词法索引（失败只打日志，不阻断入库）
        self.rebuild_lexical_index()
        return stats

    def _increment_meta(
        self, source: str, doc_id: str, doc_type: str, content_hash: str, extra: dict
    ) -> dict:
        """增量文档 metadata：统一 schema 4.3 的核心字段 + 透传额外键（None 一律丢弃）。"""
        extra = dict(extra or {})
        meta = {
            "layer": extra.get("layer") or INCREMENT_LAYER,
            "domain": extra.get("domain") or "increment",
            "doc_type": doc_type,
            "chunk_type": extra.get("chunk_type") or INCREMENT_CHUNK_TYPE,
            "category": extra.get("category") or "",
            "source": source,
            "doc_id": doc_id,
            "title": str(extra.get("title") or ""),
            "updated": str(extra.get("updated") or ""),
            "tags": _join_tags(extra.get("tags")),
            "quality": extra.get("quality") or INCREMENT_QUALITY,
            "content_hash": content_hash,
            "strategy_version": self.strategy_version,
            # v1 键名同值保留一份：下游沿用 chunking_version 的调用点不断裂
            "chunking_version": self.strategy_version,
        }
        for key, value in extra.items():
            if key not in meta:
                meta[key] = value
        return _clean_meta(meta)

    def _log_document_summary(self, stats: dict) -> None:
        """打印一行入库摘要（入了几个 chunk、是否跳过）。"""
        head = (
            f"入库摘要: source={stats['source']} doc_id={stats['doc_id']} "
            f"类型={stats['doc_type']} 策略版本={stats['strategy_version']}"
        )
        if stats.get("skipped"):
            print(
                f"{head} 跳过=是（content_hash 与 strategy_version 均未变）"
                f" 已有 chunk={stats.get('previous_chunks', 0)} 写入 chunk=0"
            )
            return
        print(
            f"{head} 跳过=否 写入 chunk={stats.get('chunks', 0)}"
            f" 删除旧 chunk={stats.get('removed', 0)} 清理孤儿={stats.get('orphans', 0)}"
        )

    def _ids_by_source(self, source: str) -> list[str]:
        """库里 metadata.source == source 的 chunk id 列表（只读，不删）。"""
        result = self.collection.get(where={"source": source})
        return list(result.get("ids") or [])

    # ── 删除 ──────────────────────────────────────────────────────

    def delete_by_source(self, source: str) -> int:
        """按 metadata.source 删除 chunk，返回删除条数；无命中返回 0。"""
        return self._delete_where({"source": source})

    def delete_by_layer(self, layer: str) -> int:
        """按 metadata.layer 清空该层，返回删除条数；无命中返回 0。"""
        return self._delete_where({"layer": layer})

    def _delete_where(self, where: dict) -> int:
        """先按条件取 ids 计数，再删除（ChromaDB delete 不返回条数）。"""
        existing = self.collection.get(where=where)
        ids = list(existing.get("ids") or [])
        if not ids:
            return 0
        self.collection.delete(ids=ids)
        return len(ids)

    # ── 内部：批量嵌入 + 写入 ──────────────────────────────────────

    def _add_chunks(self, ids: list[str], chunks: list[dict]) -> None:
        """批量嵌入 chunk 正文并写入 collection（documents 存 content，metadata 存元数据）。

        切片器返回的新增 metadata 键（heading_path / parent_id / chunk_type）随
        chunk["metadata"] 整体透传入库，这里不做白名单过滤，也不丢键。
        """
        contents = [chunk["content"] for chunk in chunks]
        metadatas = [chunk["metadata"] for chunk in chunks]
        embeddings = self.embedder.embed(contents)
        self.collection.add(
            ids=list(ids),
            embeddings=embeddings,
            documents=contents,
            metadatas=metadatas,
        )

    def _upsert_chunks(self, ids: list[str], chunks: list[dict]) -> None:
        """批量嵌入并 upsert：同 ID 覆盖而非追加，配合先删后插保证重跑幂等。"""
        contents = [chunk["content"] for chunk in chunks]
        metadatas = [chunk["metadata"] for chunk in chunks]
        embeddings = self.embedder.embed(contents)
        self.collection.upsert(
            ids=list(ids),
            embeddings=embeddings,
            documents=contents,
            metadatas=metadatas,
        )
