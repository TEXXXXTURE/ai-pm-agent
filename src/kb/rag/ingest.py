# RAG 子库 - 入库器（概念层 Markdown + 论文层 JSON → 分块 → 嵌入 → ChromaDB）
"""RAGIngest：把概念层文档与论文层数据写进 ChromaDB 的 agent_knowledge collection。

设计要点（对应《RAG知识库实施规划》4.3 / 5.3）：
- 单个 collection：agent_knowledge，余弦距离（metadata {"hnsw:space": "cosine"}）；
- 概念层：遍历目录下一级子目录（跳过 assets、点开头目录、非目录），
  每个目录 rglob("*.md") 逐篇处理；section-aware 分块由 splitter 负责；
- 论文层：整篇一块（"# 标题 + abstract"），abstract 缺失或去空白后短于 50 字符的跳过；
- 幂等：每篇文档入库前先按 metadata.source 删除旧 chunk（先删后插，重跑不翻倍）；
- metadata 值只放 str/int/float/bool（None 会让 ChromaDB 报错），
  本期没有飞书对应关系，因此不写 feishu_ref 键；
- 嵌入通过 embedder 注入：测试传假 embedder，零真实 API。
"""
from __future__ import annotations

import hashlib
import sys
import warnings
from pathlib import Path

import chromadb
import yaml

from kb.rag.embeddings import EmbeddingModel
from kb.rag.splitter import split_document

# ChromaDB collection 名（三层共用一个 collection，靠 metadata.layer 区分）
COLLECTION_NAME = "agent_knowledge"
# 分块策略版本号，写进每个 chunk 的 metadata，便于日后整库重建比对
CHUNKING_VERSION = "v1"
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


class RAGIngest:
    """领域知识库入库器（概念层 Markdown / 论文层 JSON）。

    Args:
        config: config.yaml 根 dict，内部取 config["domain_kb"]。
        embedder: 可选嵌入器；传入即用（测试注入假 embedder），未传则用
            EmbeddingModel(domain_kb["embedding"])（真实调用硅基流动 API）。
    """

    def __init__(self, config: dict, embedder=None):
        domain_kb = dict((config or {}).get("domain_kb") or {})
        self.domain_kb = domain_kb
        # 相对路径以 cwd（项目根）解析：Path 保持相对形态，交给 ChromaDB 解析
        self.chroma_path = Path(str(domain_kb.get("chroma_path") or "./domain_kb/chroma"))
        self.client = chromadb.PersistentClient(path=str(self.chroma_path))
        self.collection = self.client.get_or_create_collection(
            COLLECTION_NAME, metadata={"hnsw:space": "cosine"}
        )
        if embedder is not None:
            self.embedder = embedder
        else:
            self.embedder = EmbeddingModel(domain_kb.get("embedding") or {})

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
        """批量嵌入 chunk 正文并写入 collection（documents 存 content，metadata 存元数据）。"""
        contents = [chunk["content"] for chunk in chunks]
        metadatas = [chunk["metadata"] for chunk in chunks]
        embeddings = self.embedder.embed(contents)
        self.collection.add(
            ids=list(ids),
            embeddings=embeddings,
            documents=contents,
            metadatas=metadatas,
        )


