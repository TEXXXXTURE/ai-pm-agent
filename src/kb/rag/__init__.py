# [C 2026-09-12] RAG 子库 - AI 领域知识库（向量检索层）
"""kb.rag 子包：AI 领域知识库（concept / curated-paper / tracked-paper 三层）。

定位说明：
- 本子库服务的是「AI 领域知识」（概念解读、精选论文、追踪论文），与业务档案库
  KBStore（kb.store，管 decision/eval/case 三类项目档案）是两套独立知识体系，
  存储位置、数据模型、检索方式均不同，互不读写、互不修改。
- 本子包只新增文件，不改动 src/kb/ 下已有的 store.py / writer.py / feishu_sync.py。
- 模块分工：embeddings.py 嵌入模型封装（硅基流动 Embeddings API）、
  splitter.py 分块器；后续阶段的 ingest（入库）与 store（检索）另行追加。
"""
