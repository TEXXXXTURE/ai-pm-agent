# RAG 子库 - AI 领域知识库（向量检索层）
"""kb.rag 子包：AI 领域知识库（concept / curated-paper / tracked-paper 三层）。

定位说明：
- 本子库服务的是「AI 领域知识」（概念解读、精选论文、追踪论文），与业务档案库
  KBStore（kb.store，管 decision/eval/case 三类项目档案）是两套独立知识体系，
  存储位置、数据模型、检索方式均不同，互不读写、互不修改。
- 本子包只新增文件，不改动 src/kb/ 下已有的 store.py / writer.py / feishu_sync.py。
- 模块分工：embeddings.py 嵌入模型封装（硅基流动 Embeddings API）、
  splitter.py 分块器；后续阶段的 ingest（入库）与 store（检索）另行追加。
"""
from pathlib import Path


def build_rag_store_if_available(config: dict | None):
    """领域向量库存在且非空时构造 RAGStore，否则返回 None。

    用于流水线启动装配：新检出环境/未建库/构造异常均降级为不接领域库，
    不阻断流水线（节点侧另有检索异常兜底）。
    """
    domain_kb = dict((config or {}).get("domain_kb") or {})
    chroma_path = Path(str(domain_kb.get("chroma_path") or "./domain_kb/chroma"))
    if not chroma_path.exists():
        return None
    try:
        from kb.rag.store import RAGStore  # 延迟导入：避免导入 nodes 包时拉起 chromadb

        store = RAGStore(config or {})
        if int(store.collection.count() or 0) == 0:
            return None
        return store
    except Exception:
        return None


# 领域知识库启动装配（库缺失/为空/异常均降级）
