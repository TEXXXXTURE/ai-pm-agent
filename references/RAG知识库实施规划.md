# AI Agent 领域知识库 RAG 实施规划

> 本文档是 RAG 知识库系统的完整实施规划，面向 Code 侧开发。
> 依据：选型结论 + 讨论细化（分类映射、持续更新 Chunk 策略）+ 2026-09-12 修订（嵌入模型 Doubao→硅基流动，规避 Agent Plan 合规红线；实施方式：派 CLI Agent 做骨架、主 Agent 验收；与主线 12 段融合工作流并行推进）+ 2026-09-12 修订（嵌入模型定为 Qwen/Qwen3-Embedding-8B 4096 维，真机实测通过；论文层种子从 40 篇扩为 514 篇全量；用户拍板质量优先）
> 性质：功能模块开发，需走工作流报批

---

## 一、项目背景与目标

### 1.1 背景

- 项目已有一套 PM 业务档案库（纯关键词检索，KBStore/KBWriter）
- 需要新建一套 AI Agent 领域知识库（向量检索 + 分层加权混合检索）
- 两套知识库底层独立，上层统一入口
- 知识来源：三层互补结构（概念解读层 / 精选论文层 / 新论文追踪层）
- 飞书知识地图作为导航层，与 RAG 库结构一一对应

### 1.2 目标

搭建一套可运行的 RAG 知识库骨架，包含：
1. ✅ 概念层文档入库（section-aware 分块 + 向量嵌入 + ChromaDB）
2. ✅ 精选论文层入库（整篇 abstract + 向量嵌入 + ChromaDB）
3. ✅ 分层加权混合检索（关键词 + 向量 + 层加权 + 分类过滤）
4. ✅ 命令行工具（入库 CLI + 检索 CLI）
5. ✅ 单元测试 + 真机冒烟测试通过

### 1.3 范围（本期不做）

- 追踪层自动更新（arXiv API + LLM 过滤）→ 留到下一阶段
- 飞书集成与同步 → 留到飞书知识地图阶段
- Reranker 重排序 → 规模大了再加
- 定时任务调度 → 稳定后再加

---

## 二、三层知识体系

### 2.1 三层结构

| 层 | 来源 | 内容形式 | 数量 | 权重 | 更新频率 | 分块方式 |
|----|------|---------|------|------|---------|---------|
| 概念解读层 | agentic-ai-knowledge-base | Markdown 文档（人写的深度解读） | 209 篇，32 分类 | ×2.0 | 每季度手动 | Section-aware 分块 |
| 精选论文层 | awesome-llm-agent-papers | 论文元数据 + abstract | 514 篇，10 分类 | ×1.5 | 每月 diff | 整篇一个 chunk |
| 新论文追踪层 | arXiv API + LLM 过滤 | 论文元数据 + abstract | 约 3000-5000 篇 | ×1.0 | 每周自动 | 整篇一个 chunk |

### 2.2 三层互补关系

```
概念解读层（人写的、有组织、有框架）
    │ 引用论文，但本身是解读
    ▼
精选论文层（人工精选、有注释、有分类）
    │ 子集关系，精选自更大的论文池
    ▼
新论文追踪层（自动采集、质量参差、覆盖前沿）
```

- 概念层回答"是什么、怎么分类、有什么区别"
- 精选层用于论文溯源、准确出处
- 追踪层覆盖最新进展，查漏补缺

### 2.3 统一分类体系

三层各有各的分类体系，映射到一套统一分类（共 ~25 个），作为 `category` 字段：

| 飞书一级 | 统一 category | 说明 |
|---------|-------------|------|
| 一、基础概念与分类 | `concepts` / `surveys` | 基础概念 / 综述论文 |
| 二、架构与核心模式 | `planning` / `memory` / `tool-use` / `multi-agent` / `evaluation` / `architecture` | 核心组件 |
| 三、框架与平台 | `frameworks` / `platforms` / `harness` | 框架与基础设施 |
| 四、应用与场景 | `coding` / `research` / `product-pm` / `environments` / `applications-other` | 各应用场景 |
| 跨领域 | `safety` / `observability` / `production` / `standards` / `context-engineering` / `prompt-engineering` / `rag` / `benchmarks` / `maturity-models` | 跨领域主题 |
| 厂商专区 | `vendor-anthropic` / `vendor-openai` / `vendor-google` / `vendor-microsoft` / `vendor-aws` | 厂商特定内容 |

详细映射关系见：`references/RAG知识库_三层分类映射表.md`

---

## 三、技术选型确认

| 组件 | 选型 | 理由 |
|------|------|------|
| 向量数据库 | ChromaDB（嵌入式） | 规模匹配（几千个 chunk）、零运维、Python 原生、支持 metadata 过滤 |
| 嵌入模型 | 硅基流动 `Qwen/Qwen3-Embedding-8B`（4096 维） | MTEB 多语言榜第一（70.58）；真机实测通过（4096 维、批量 32 条 0.84s、约 4 字符/token）；全量语料约 62 万 tokens，单轮嵌入成本约 ¥0.2，质量优先无成本负担 |
| 分块策略 | 按层差异化（概念层 section-aware / 论文层整篇） | 不同内容性质适配不同分块方式 |
| 检索策略 | 分层加权混合检索 | 兼顾语义和精确，高质量内容优先 |
| 更新机制 | 按层差异化频率（季/月/周） | 各层变化速度不同，节奏匹配 |

---

## 四、架构设计

### 4.1 模块划分

```
src/kb/
├── __init__.py          （已有，不动）
├── store.py             （已有，不动 — 业务档案库 KBStore）
├── writer.py            （已有，不动 — 业务档案库 KBWriter）
├── feishu_sync.py       （已有，不动，占位）
└── rag/                 【新增】RAG 知识库模块
    ├── __init__.py
    ├── store.py         RAGStore：分层加权混合检索
    ├── ingest.py        RAGIngest：文档分块 + 嵌入 + 入库
    ├── splitter.py      分块器：section-aware + 整篇 chunk
    └── embeddings.py    嵌入模型封装：Doubao-embedding API

scripts/
├── kb_query.py          （已有，后续扩展 --domain 参数）
├── rag_ingest.py        【新增】入库 CLI
└── rag_query.py         【新增】检索 CLI
```

### 4.2 数据目录

```
domain_kb/
├── chroma/              ChromaDB 数据目录（嵌入式，自动创建）
├── source/              原始文档源文件
│   ├── concept/         概念解读层 Markdown 文档（32 个分类目录）
│   ├── curated-papers/  精选论文层（JSON 格式）
│   └── tracked-papers/  新论文追踪层（本期不做，留空）
└── update_logs/         更新日志（本期不做，留空）
```

### 4.3 ChromaDB Collection 设计

- 单个 collection：`agent_knowledge`
- 每个 chunk 的 metadata 字段：

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | str | 全局唯一 ID（见下方 ID 规则） |
| `layer` | str | `concept` / `curated-paper` / `tracked-paper` |
| `category` | str | 统一分类名（如 `planning`、`memory`） |
| `source` | str | 来源标识（文件路径 / arxiv_id） |
| `title` | str | 文档标题 |
| `curated` | bool | 是否人工精选 |
| `feishu_ref` | str \| null | 对应飞书知识点 ID |
| `chunk_index` | int | 文档内 chunk 序号（论文层恒为 0） |
| `section_title` | str | 所属小节标题（概念层有，论文层为空） |
| `chunking_version` | str | 分块策略版本号（如 "v1"） |
| `source_category` | str | 原始分类（目录名 / 章节名 / arXiv 分类） |

**Chunk ID 规则**：
```
概念层：  concept-{source_path_hash}-{chunk_index}
精选层：  curated-{arxiv_id}
追踪层：  tracked-{arxiv_id}
```

---

## 五、核心模块设计

### 5.1 embeddings.py — 嵌入模型封装

**接口**：
```python
class EmbeddingModel:
    def __init__(self, config: dict): ...
    def embed(self, texts: list[str]) -> list[list[float]]:
        """批量嵌入，返回向量列表。"""
    def embed_query(self, text: str) -> list[float]:
        """嵌入单条查询。"""
```

**实现要点**：
- 直接调用硅基流动 Embeddings API（OpenAI 兼容格式）<!-- 原方案 Doubao 弃用：项目 ARK_API_KEY 是火山 Agent Plan 专属 key，按量端点 /api/v3 不认（401），且 Agent Plan 向量模型禁止脚本裸调（合规红线） -->
- 批量请求（每批上限 32 条，实测通过）
- 失败重试 2 次（指数退避）
- API 端点：`https://api.siliconflow.cn/v1/embeddings`
- 模型名：`Qwen/Qwen3-Embedding-8B`（4096 维；真机实测通过：单条 0.32s、批量 32 条 0.84s，已定档，建库即用此模型）
- 密钥环境变量：`SILICONFLOW_API_KEY`（已入项目 .env，2026-09-12）
- 账户有余额：不锁免费档；模型已选定（Qwen3-Embedding-8B），可建库

### 5.2 splitter.py — 分块器

**接口**：
```python
def split_document(
    text: str,
    doc_type: str,         # "concept" | "paper"
    metadata: dict,
) -> list[dict]:
    """返回 chunk 列表，每项含 content + metadata（含 chunk_index / section_title）"""
```

**分块策略**：

| 文档类型 | 分块方式 | chunk 大小 | 说明 |
|---------|---------|-----------|------|
| concept | Section-aware | 500-800 字，overlap 100 | 按 Markdown 标题层级切，每个小节一块，超长二次切分 |
| paper | 整篇一块 | 整篇 abstract | abstract 短，语义完整，不切 |

**Section-aware 实现思路**：
1. 先剥离 YAML front matter
2. 按 `#` / `##` / `###` 标题拆分
3. 每个标题块 = 一个基础 chunk，记录 section_title
4. 单个块超过 800 字的，按段落二次拆分（500-800 字，overlap 100）

### 5.3 ingest.py — 入库器

**接口**：
```python
class RAGIngest:
    def __init__(self, config: dict): ...

    def ingest_concept_dir(self, dir_path: str) -> int:
        """批量导入概念层 Markdown 文档目录，返回 chunk 数。"""

    def ingest_papers(
        self,
        papers: list[dict],
        layer: str = "curated-paper",
    ) -> int:
        """批量导入论文层（JSON 列表），返回 chunk 数。"""

    def delete_by_source(self, source: str) -> int:
        """按 source 删除 chunk（文档更新时先删后插）。"""

    def delete_by_layer(self, layer: str) -> int:
        """清空整个层（全量重建用）。"""
```

**入库流程**：
```
读取源文件 → 提取元数据 → 分类映射 → 分块 → 批量嵌入 → 写入 ChromaDB → 记录
```

**分类映射**：
- 概念层：目录名 → 统一分类（按映射表，大部分可直接映射，少数需按 front matter tags 辅助判断）
- 精选层：章节名 → 统一分类（10 个章节直接映射）
- 映射表位置：代码内硬编码（一个 dict），后续可移到配置文件

### 5.4 store.py — 检索器

**接口**：
```python
class RAGStore:
    def __init__(self, config: dict): ...

    def search(
        self,
        query: str,
        top_k: int = 5,
        layers: list[str] | None = None,
        category: str | None = None,
        feishu_refs: list[str] | None = None,
    ) -> list[dict]:
        """分层加权混合检索，返回按加权分数降序的结果列表。"""
```

**检索完整流程**：

```
查询
  │
  ├── 1. 向量检索 → 候选集 B（top_k * 5，保证召回量）
  │     （先用向量捞一批，再在候选里做关键词打分，避免全库扫描）
  │
  ├── 2. 在候选集中做关键词打分 → 结果集 A
  │     复用 kb/store.py 的 _tokenize 分词逻辑
  │     分数归一化到 0-1
  │
  ├── 3. 合并（同一个 chunk 同时有关键词分 + 向量分）
  │
  ├── 4. 计算融合分
  │     final_score = 0.4 × keyword_norm + 0.6 × vector_sim
  │
  ├── 5. 分层加权
  │     weighted_score = final_score
  │       × layer_weight  （concept×2.0 / curated×1.5 / tracked×1.0）
  │       × (1.2 if curated else 1.0)
  │       × (1.3 if feishu_ref hit else 1.0)
  │
  └── 6. 排序 → 返回 Top-K
```

**权重配置（config.yaml 可调整）**：
- `keyword_weight`: 0.4
- `vector_weight`: 0.6
- `layer_weights`: {concept: 2.0, curated-paper: 1.5, tracked-paper: 1.0}
- `curated_bonus`: 1.2
- `feishu_ref_bonus`: 1.3

---

## 六、CLI 工具

### 6.1 rag_ingest.py — 入库工具

```bash
# 导入概念层（整个目录）
python scripts/rag_ingest.py --layer concept --source domain_kb/source/concept/

# 导入精选论文（JSON 文件）
python scripts/rag_ingest.py --layer curated-paper --source domain_kb/source/curated-papers/papers_with_abstract.json

# 清空某层后重新导入
python scripts/rag_ingest.py --layer concept --source ... --clean
```

**参数**：
- `--layer`：知识层（concept | curated-paper | tracked-paper）
- `--source`：源路径（目录或 JSON 文件）
- `--clean`：入库前清空该层
- `--config`：配置文件路径

### 6.2 rag_query.py — 检索工具

```bash
# 基础查询
python scripts/rag_query.py --query "什么是 ReAct"

# 限定层 + 分类
python scripts/rag_query.py --query "planning" --layers concept --category planning

# JSON 输出
python scripts/rag_query.py --query "Agent 分类" --json
```

**参数**：
- `--query`：查询文本（必填）
- `--layers`：知识层过滤
- `--category`：分类过滤
- `--top-k`：返回数量（默认 5）
- `--json`：输出 JSON 格式

---

## 七、配置变更

### 7.1 config.yaml 新增段

```yaml
domain_kb:
  chroma_path: ./domain_kb/chroma
  source_path: ./domain_kb/source

  embedding:
    provider: siliconflow
    model: Qwen/Qwen3-Embedding-8B
    api_key_env: "SILICONFLOW_API_KEY"
    api_base: "https://api.siliconflow.cn/v1"
    dimensions: 4096
    batch_size: 32

  retrieval:
    top_k: 5
    keyword_weight: 0.4
    vector_weight: 0.6
    layer_weights:
      concept: 2.0
      curated-paper: 1.5
      tracked-paper: 1.0
    curated_bonus: 1.2
    feishu_ref_bonus: 1.3
```

### 7.2 requirements.txt 新增

```
chromadb>=0.5
```

---

## 八、种子数据（已准备好）

| 层 | 状态 | 位置 |
|----|------|------|
| 概念解读层 | ✅ 已下载 | `domain_kb/source/concept/`（209 篇，32 分类） |
| 精选论文层 | ⚠️ 元数据就绪，abstract 补全中（已后台跑全量 514 篇，约 27 分钟） | `domain_kb/source/curated-papers/papers_raw.json`（514 篇）→ `papers_with_abstract.json` |
| 新论文追踪层 | ❌ 未开始 | 本期不做 |

**精选层 abstract 补充策略**（从 40 篇扩为全量 514 篇，用户拍板）：
- 全量 514 篇补 abstract（arXiv 免费，限速 3 秒/篇，约 27 分钟，`fetch_abstracts.py --limit 0` 后台跑，支持断点续传）
- 脚本已就绪：`domain_kb/source/curated-papers/fetch_abstracts.py`（已去交互化改 `--limit` 参数）

---

## 九、实施步骤与里程碑

### 阶段一：基础框架（预估 1 个会话）

| 步骤 | 内容 | 产出 |
|------|------|------|
| 1 | 配置 + 依赖（config.yaml 新增 domain_kb 段 + requirements.txt 加 chromadb） | 配置就绪 |
| 2 | 实现 embeddings.py（嵌入模型封装） | 嵌入模型可调用 |
| 3 | 实现 splitter.py（分块器） | 分块器可用 |
| 4 | 单元测试：splitter | 分块逻辑验证 |

### 阶段二：入库器 + 检索器（预估 1-2 个会话）

| 步骤 | 内容 | 产出 |
|------|------|------|
| 5 | 实现 ingest.py（入库器） | 可导入概念层和论文层 |
| 6 | 实现 store.py（检索器） | 分层加权混合检索可用 |
| 7 | 单元测试：ingest + store（mock ChromaDB + embedding） | 逻辑验证 |

### 阶段三：CLI + 真机测试（预估 1 个会话）

| 步骤 | 内容 | 产出 |
|------|------|------|
| 8 | 实现 rag_ingest.py CLI | 命令行可入库 |
| 9 | 实现 rag_query.py CLI | 命令行可检索 |
| 10 | 补精选层 abstract（40 篇种子） | 精选层种子数据 |
| 11 | 真机冒烟测试：导入概念层 + 精选层 → 跑几个查询验证 | 端到端跑通 |

### 里程碑

- **M1**：单元测试全绿（步骤 4 + 7）
- **M2**：真机冒烟通过（步骤 11）→ 骨架可用

---

## 十、风险与注意事项

1. **嵌入渠道已定稿（硅基流动 Qwen/Qwen3-Embedding-8B，4096 维，真机实测通过；用户拍板质量优先，弃 bge-m3 候选）**：已实测端点、模型、维度、批量（32 条）；全量嵌入成本约 ¥0.2/轮；ChromaDB 建库后维度固定，嵌入模型中途更换需整库重建
2. **ChromaDB 版本锁定**：安装时锁定主版本，避免 API 不兼容
3. **概念层分类映射**：32 个目录映射到统一分类，大部分能按目录名直接映射，少数（如 AgenticFrameworks 下内容跨分类）可能需要按文件内容或 tags 辅助判断
4. **arXiv API 限速**：补 abstract 时遵守每 3 秒 1 次的限制
5. **嵌入成本**：账户有余额按量计费（嵌入单价低），三层全量约 4000-6500 个嵌入成本在几元以内；模型选定后回填实际单价

---

## 十一、后续阶段（本期不做）

1. **追踪层自动更新**：arXiv API 每周拉取 + LLM 过滤 + 自动入库
2. **精选层每月 diff 更新**：自动对比 awesome 列表变化
3. **飞书知识地图集成**：飞书路由 + feishu_ref 加权
4. **kb_query.py 统一入口**：扩展 `--domain` 参数，两套知识库统一查询
5. **LangGraph 集成**：RAG 检索作为工具节点挂到工作流
6. **查询感知动态权重**：根据查询类型调整各层权重
7. **Reranker 精排**：规模大了之后加

---

## 相关文档

- 《AI Agent 知识库 RAG 选型笔记》—— 8 项选型决策过程
- 《RAG知识库_三层分类映射表》—— 三层分类到统一分类的映射
- 《RAG知识库实施指南_自动更新篇》—— 持续更新机制（本期实现基础，留接口）
- 《飞书知识地图_目录结构设计》—— 飞书侧结构（后续阶段）

<!-- RAG 知识库实施规划（最终版） -->
<!-- 用户拍板：嵌入模型 Doubao→硅基流动 bge-m3（SILICONFLOW_API_KEY 已入 .env）；实施派 CLI Agent（CodeBuddy hy3）做骨架、主 Agent 验收；与主线并行，RAG 会话开场词「rag」、主线会话开场词「继续」 -->
