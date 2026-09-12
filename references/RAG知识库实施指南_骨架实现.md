# AI Agent 领域知识库 RAG 实施指南 — 骨架实现篇

> 本文档是 W01 选型结论的落地实施方案，面向 Code 侧实现。
> 目标：从零搭建一套可运行的 RAG 知识库骨架，包含入库、检索、CLI 工具三部分。
> 对应选型文档：《AI Agent 知识库 RAG 选型笔记》

---

## 一、整体架构

### 1.1 两套知识库并存

```
项目知识库
├── PM 业务档案库（已有）        src/kb/store.py + writer.py
│   └── 纯关键词检索，decision/eval/case 三类档案
└── AI Agent 领域知识库（新建）   src/kb/rag/ 新模块
    └── 向量检索 + 分层加权混合检索，concept/curated-paper/tracked-paper 三层
```

两套知识库**底层独立、上层统一入口**：
- 业务档案库保持不变（`kb_store/` 目录，`KBStore` / `KBWriter`）
- 领域知识库新建独立模块（`domain_kb/` 目录，`RAGStore` / `RAGIngest`）
- 查询入口通过 `scripts/kb_query.py` 扩展 `--domain` 参数统一调度

### 1.2 新增文件清单

```
src/kb/
├── __init__.py          （已有，不动）
├── store.py             （已有，不动 — 业务档案库）
├── writer.py            （已有，不动）
├── feishu_sync.py       （已有，不动，占位）
└── rag/
    ├── __init__.py
    ├── store.py         RAGStore：分层加权混合检索
    ├── ingest.py        RAGIngest：文档分块 + 嵌入 + 入库
    ├── splitter.py      分块器：section-aware + 整篇 chunk
    └── embeddings.py    嵌入模型封装：Doubao-embedding API

scripts/
├── kb_query.py          （已有，扩展 --domain 参数）
├── rag_ingest.py        新增：入库 CLI
└── rag_query.py         新增：检索 CLI（独立工具，也可被 kb_query 调用）

config.yaml              新增 domain_kb 配置段
requirements.txt         新增 chromadb 依赖
```

### 1.3 数据目录

```
domain_kb/
├── chroma/              ChromaDB 数据目录（嵌入式，自动创建）
├── source/              原始文档源文件（按层分子目录）
│   ├── concept/         概念解读层 Markdown 文档
│   ├── curated-papers/  精选论文层（JSON 格式元数据 + abstract）
│   └── tracked-papers/  新论文追踪层（同精选层格式）
└── ingest_log.jsonl     入库日志（记录每次入库的文档 ID 和时间）
```

---

## 二、配置设计（config.yaml 新增段）

在 `config.yaml` 中新增：

```yaml
domain_kb:
  # ChromaDB 数据目录（相对路径以项目根为基准）
  chroma_path: ./domain_kb/chroma
  # 原始文档源目录
  source_path: ./domain_kb/source
  # 嵌入模型配置
  embedding:
    provider: doubao         # 嵌入模型 provider 名
    model: doubao-embedding  # 模型名（LiteLLM / API 模型名）
    api_key_env: "ARK_API_KEY"  # 密钥环境变量名
    api_base: "https://ark.cn-beijing.volces.com/api/v3"  # 火山引擎 Ark 端点
    dimensions: 2560         # 向量维度
  # 检索配置
  retrieval:
    top_k: 5
    keyword_weight: 0.4      # 关键词检索权重
    vector_weight: 0.6       # 向量检索权重
    layer_weights:            # 各知识层权重
      concept: 2.0
      curated-paper: 1.5
      tracked-paper: 1.0
    curated_bonus: 1.2       # curated 标记额外加权
    feishu_ref_bonus: 1.3    # 飞书路由命中额外加权
```

同时在 `llm.providers` 下注释模板中增加 doubao embedding 示例（注意：embedding 不经过 ChatLiteLLM，走直接 API 调用）。

---

## 三、核心模块设计

### 3.1 embeddings.py — 嵌入模型封装

**职责**：统一封装嵌入模型调用，支持批量嵌入。

**接口**：
```python
class EmbeddingModel:
    def __init__(self, config: dict): ...
    def embed(self, texts: list[str]) -> list[list[float]]:
        """批量嵌入，返回向量列表。"""
    def embed_query(self, text: str) -> list[float]:
        """嵌入单条查询（与 embed 一致，便利方法）。"""
```

**实现要点**：
- 直接调用火山引擎 Ark 的 Embeddings API（OpenAI 兼容格式）
- 批量请求，单次最多 2048 个 token 或 25 条文本（按 API 限制）
- 失败重试 2 次（指数退避）
- **不经过 LiteLLM**：LiteLLM 的 embedding 支持不如直接调 API 稳定，且我们只需要这一个 embedding 模型

**API 调用方式**：
```
POST https://ark.cn-beijing.volces.com/api/v3/embeddings
Headers: Authorization: Bearer {ARK_API_KEY}
Body: {"model": "doubao-embedding", "input": ["text1", "text2"]}
```

### 3.2 splitter.py — 分块器

**职责**：按文档类型选择不同的分块策略。

**接口**：
```python
def split_document(
    text: str,
    doc_type: str,         # "concept" | "paper"
    metadata: dict,        # 文档元数据
) -> list[dict]:
    """
    返回 chunk 列表，每个 chunk 含：
    - content: str         块文本内容
    - metadata: dict       合并后的元数据（含 chunk_index / section_title 等）
    """
```

**分块策略**：

| 文档类型 | 分块方式 | chunk 大小 | 说明 |
|---------|---------|-----------|------|
| concept（概念文档） | Section-aware | 500-800 字，overlap 100 | 按 Markdown 标题层级切，每个小节一块，超长再做二次切分 |
| paper（论文） | 整篇一块 | 整篇 abstract | 单篇论文的 abstract 本身就短，不切 |

**Section-aware 分块实现思路**：
1. 按 `#` / `##` / `###` 标题拆分文档
2. 每个标题块作为一个基础 chunk
3. 若单个块超过 800 字，按段落二次拆分，每段 ~500-800 字，overlap 100
4. chunk 的 metadata 中记录 `section_title`（当前小节标题）和 `chunk_index`

### 3.3 ingest.py — 入库器

**职责**：读取源文档 → 分块 → 嵌入 → 写入 ChromaDB。

**接口**：
```python
class RAGIngest:
    def __init__(self, config: dict): ...

    def ingest_concept_docs(self, docs_dir: str) -> int:
        """批量导入概念层 Markdown 文档，返回入库 chunk 数。"""

    def ingest_papers(
        self,
        papers: list[dict],
        layer: str = "curated-paper",  # curated-paper | tracked-paper
    ) -> int:
        """批量导入论文层（JSON 格式列表），返回入库 chunk 数。"""

    def delete_by_source(self, source: str) -> int:
        """按 source 字段删除已有 chunk（用于更新前清理旧版本）。"""
```

**ChromaDB collection 设计**：
- 只用一个 collection：`agent_knowledge`
- 每个 chunk 的 metadata 必含字段：
  - `id`: str — 全局唯一 ID（格式见下方 ID 设计规则）
  - `layer`: str — `concept` | `curated-paper` | `tracked-paper`
  - `category`: str — 统一分类（如 "planning"、"memory"、"tool-use"）
  - `source`: str — 来源标识（文件名或 arXiv ID）
  - `title`: str — 文档标题
  - `curated`: bool — 是否人工精选（精选层 = true，追踪层 = false）
  - `feishu_ref`: str | None — 对应飞书知识点的 ID（可选）
  - `chunk_index`: int — 文档内的 chunk 序号（论文层恒为 0）
  - `section_title`: str — 所属小节标题（概念层有，论文层为空）
  - `chunking_version`: str — 分块策略版本号（如 "v1"），用于判断是否需要全量重建
  - `source_category`: str — 原始分类（概念层的目录名 / 精选层的章节名 / 追踪层的 arXiv 分类）

**Chunk ID 设计规则**：
```
概念层：  concept-{source_file_path_hash}-{chunk_index}
精选层：  curated-{arxiv_id}
追踪层：  tracked-{arxiv_id}
```
- 概念层用文件路径 hash + chunk 索引，保证同一文件同一分块策略下 ID 稳定
- 论文层直接用 arxiv_id，简单好记，跨层迁移时 ID 前缀变一下就行

**入库流程**：
```
读取源文件 → 解析元数据 → 分块 → 批量嵌入 → 写入 ChromaDB → 记录入库日志
```

### 3.4 store.py — 检索器

**职责**：实现分层加权混合检索。

**接口**：
```python
class RAGStore:
    def __init__(self, config: dict): ...

    def search(
        self,
        query: str,
        top_k: int = 5,
        layers: list[str] | None = None,     # 限定知识层
        category: str | None = None,         # 限定分类（飞书路由预判）
        feishu_refs: list[str] | None = None, # 飞书路由命中的知识点 ID 列表
    ) -> list[dict]:
        """
        分层加权混合检索，返回按加权分数降序的结果列表。
        每项含：id / layer / category / title / source / content / score / metadata
        """
```

**检索完整流程**：

```
查询输入
  │
  ├── 1. 分类预过滤（可选）
  │     若提供 category，只在对应 category 中检索
  │
  ├── 2. 关键词检索 → 结果集 A
  │     复用 kb.store._tokenize 的中英文分词
  │     在所有 chunk 文本中做关键词命中打分
  │     分数归一化到 0-1
  │
  ├── 3. 向量语义检索 → 结果集 B
  │     查询文本 → 嵌入 → ChromaDB 相似度搜索
  │     返回 top_k * 3（留够融合空间）
  │     余弦相似度本身就是 0-1
  │
  └── 4. 合并去重
         A + B 合并，同一个 chunk 只保留一次
         同时记录 keyword_score 和 vector_score
         │
         5. 计算融合分数
         final_score = keyword_weight × keyword_norm
                    + vector_weight × vector_sim
         （keyword_weight=0.4, vector_weight=0.6）
         │
         6. 分层加权
         weighted_score = final_score
           × layer_weight  （concept×2.0 / curated×1.5 / tracked×1.0）
           × (curated_bonus if curated else 1.0)
           × (feishu_ref_bonus if id in feishu_refs else 1.0)
         │
         7. 排序，返回 Top-K
```

**关键词检索实现说明**：
- 分词方式复用 `kb/store.py` 中的 `_tokenize()` 函数（中文二元组 + 英文词）
- 打分方式：chunk 文本中 query token 的命中次数
- 归一化：除以所有结果中的最高分（若全 0 分则跳过该 chunk）
- 注意：关键词检索不需要全部 chunk 扫描，可以先让 ChromaDB 返回一批候选（如 top_k * 5），再在候选中做关键词打分

**建议实现方式**：为了性能，关键词检索不做全库扫描，而是：
1. 先向量检索返回 top_k * 5 个候选
2. 在候选中做关键词打分
3. 然后融合、加权、排序
4. 这样既保证了召回（向量先捞），又控制了计算量

---

## 四、CLI 工具设计

### 4.1 rag_ingest.py — 入库工具

**用法**：
```bash
# 导入概念层文档
python scripts/rag_ingest.py --layer concept --source domain_kb/source/concept/

# 导入精选论文（JSON 文件）
python scripts/rag_ingest.py --layer curated-paper --source domain_kb/source/curated-papers/papers.json

# 全量重新导入（先清后写）
python scripts/rag_ingest.py --layer concept --source ... --clean
```

**参数**：
- `--layer`：知识层（concept | curated-paper | tracked-paper）
- `--source`：源文件路径（目录或 JSON 文件）
- `--clean`：入库前清空该层的旧数据（默认增量追加）
- `--config`：配置文件路径（默认项目根 config.yaml）

### 4.2 rag_query.py — 检索工具

**用法**：
```bash
# 基础查询
python scripts/rag_query.py --query "什么是 ReAct"

# 限定层 + 分类
python scripts/rag_query.py --query "planning 模式" --layers concept --category planning

# 指定返回数量
python scripts/rag_query.py --query "Agent 分类" --top-k 10
```

**参数**：
- `--query`：查询文本（必填）
- `--layers`：知识层，可重复指定或逗号分隔
- `--category`：分类过滤
- `--top-k`：返回数量（默认 5）
- `--json`：输出 JSON 格式（默认人类可读格式）

**输出格式（人类可读）**：
```
查询：什么是 ReAct
共命中 5 条结果

[1] 得分 0.85 [concept ×2.0] Agent 分类体系 / 按自主程度分
    ReAct（Yao et al. 2022, ICLR 2023）是一种...
    来源：agentic-ai-knowledge-base / agent-types.md

[2] 得分 0.72 [curated-paper ×1.5] ReAct: Synergizing Reasoning and Acting...
    ...
    来源：awesome-llm-agent-papers / arXiv:2210.03629
...
```

### 4.3 kb_query.py 扩展

在现有 `scripts/kb_query.py` 中增加 `--domain` 参数：
- `--domain archive`（默认）：查业务档案库，走原逻辑
- `--domain agent`：查 AI Agent 领域知识库，调用 `RAGStore`
- `--domain both`：两套都查，合并返回（暂不实现，留接口）

---

## 五、种子数据准备

### 5.1 概念层种子数据

**来源**：agentic-ai-knowledge-base 项目的 Markdown 文档

**获取方式**：
- GitHub 仓库：https://github.com/agentic-ai-knowledge-base/agentic-ai-knowledge-base
- 克隆或下载 docs/ 目录下的 Markdown 文件

**目录结构预期**：
```
domain_kb/source/concept/
├── 01_concepts/
│   ├── what-is-agent.md
│   ├── agent-vs-workflow.md
│   └── autonomy-spectrum.md
├── 02_architecture/
│   ├── planning.md
│   ├── memory.md
│   └── tool-use.md
└── ...
```

**首批导入建议**：先导入 10-20 篇核心文档做测试，不全量导入。

### 5.2 精选论文层种子数据

**来源**：awesome-llm-agent-papers 的 README.md + 补全 abstract

**格式**：JSON 数组，每篇论文一个对象：
```json
[
  {
    "arxiv_id": "2210.03629",
    "title": "ReAct: Synergizing Reasoning and Acting in Language Models",
    "authors": ["Shunyu Yao", ...],
    "year": 2022,
    "abstract": "论文摘要全文...",
    "category": "reasoning",
    "tags": ["ReAct", "reasoning", "tool-use"],
    "url": "https://arxiv.org/abs/2210.03629"
  },
  ...
]
```

**首批建议**：先导入 20-30 篇经典论文做测试。

### 5.3 追踪层（暂不实现）

追踪层需要 arXiv API + LLM 过滤，留到自动更新机制那一篇再详细设计。
骨架实现阶段可以跳过，先把概念层和精选层跑通。

---

## 六、测试方案

### 6.1 单元测试

```
tests/test_rag_splitter.py    分块器测试（section-aware 边界、论文整篇）
tests/test_rag_embedding.py   嵌入模型测试（mock API，测试批量逻辑）
tests/test_rag_store.py       检索器测试（mock ChromaDB，测试加权逻辑）
tests/test_rag_ingest.py      入库器测试（mock embedding + ChromaDB）
```

### 6.2 集成测试

用 5-10 篇真实文档做端到端测试：
1. 入库 → 检查 chunk 数量和元数据
2. 检索 → 输入已知查询，验证 Top-3 是否包含预期结果
3. 加权验证 → 验证 concept 层的内容是否排在 curated 层前面

### 6.3 真机冒烟测试

```bash
# 1. 安装依赖
pip install chromadb

# 2. 准备少量种子数据（3-5 篇概念文档）

# 3. 入库
python scripts/rag_ingest.py --layer concept --source domain_kb/source/concept/

# 4. 检索测试
python scripts/rag_query.py --query "什么是 Workflow"
```

---

## 七、实施步骤建议（Code 侧执行顺序）

| 步骤 | 内容 | 预计工作量 | 依赖 |
|------|------|-----------|------|
| 1 | 新增 config.yaml domain_kb 段 + requirements.txt chromadb | 小 | 无 |
| 2 | 实现 embeddings.py（嵌入模型封装） | 中 | 步骤 1 |
| 3 | 实现 splitter.py（分块器） | 小 | 无 |
| 4 | 实现 ingest.py（入库器） | 中 | 步骤 2、3 |
| 5 | 实现 store.py（检索器） | 大 | 步骤 2 |
| 6 | 实现 rag_ingest.py CLI | 小 | 步骤 4 |
| 7 | 实现 rag_query.py CLI | 中 | 步骤 5 |
| 8 | 准备种子数据 + 真机冒烟测试 | 中 | 步骤 6、7 |
| 9 | 单元测试补齐 | 中 | 步骤 2-5 |
| 10 | kb_query.py 扩展 --domain | 小 | 步骤 5 |

**里程碑**：步骤 8 完成 = 骨架可用，能入库能检索。

---

## 八、风险与注意事项

1. **嵌入模型 API 可用性**：火山引擎 Ark 的 embedding 接口需确认具体模型名和端点，实施前先做 API 连通性测试
2. **ChromaDB 版本兼容性**：安装时锁定主版本，避免 API 变更
3. **中文分块准确性**：section-aware 分块依赖 Markdown 标题格式，源文档格式不统一时可能出错，增加容错
4. **加权公式初始值**：所有权重都是设计值，首次真机测试后根据实际效果调整
5. **向量维度一致性**：嵌入模型的维度（2560）必须和 ChromaDB collection 的维度一致，切换模型时需重建库

---

## 相关文档

- 《AI Agent 知识库 RAG 选型笔记》—— 选型决策过程
- 《RAG 知识库实施指南 — 自动更新篇》—— 待写
- 《RAG 知识库实施指南 — 飞书集成篇》—— 待写

<!-- [W02 2026-09-12] RAG 知识库骨架实施方案 -->
