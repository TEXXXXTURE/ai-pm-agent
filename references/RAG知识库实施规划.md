# AI Agent 领域知识库 RAG 实施规划（v2）

> **版本**：v2（2026-10-15 重构）
> **修订记录**：
> - v1（2026-09-12）：三层知识（concept/curated/tracked）骨架；ChromaDB + 硅基流动 Qwen3-Embedding-8B（4096 维，真机实测通过）；向量召回 + 候选内关键词分的分层加权混合检索。
> - v2（2026-10-15）：按产品定调重构——语料重组为**增量/存量双域**；增量侧新增**格式契约 + 实时幂等入库**；存量侧新增**社区讨论帖、模型信息**两类语料的类型化切片；检索**推翻重建为双路召回（bm25s ∥ 向量）+ RRF 融合**。决策过程见《AI_Agent_知识库_RAG选型笔记》文末「选型复审」章节。
> - v2.1（2026-10-15）：五轮定调入账——**存量更新 = 低频「整理→重切」**（不禁止重切，只是间隔长于增量）；全库重建触发口径扩展、frozen 语义收窄，见选型笔记「第五轮定调」。
> **性质**：功能模块开发，需走工作流报批。**待拍板项**（见复审文档末尾）未定前，阶段二/四按推荐默认值 a 写，拍板后按结论调整。
> **关联文档**：选型笔记（决策过程）→ 本规划（实施）→ 实施指南两篇（调优/更新，其中与 v2 冲突处以本规划为准，文首已标注）。

---

## 〇、路线图快照（2026-10-15 落盘，执行期以此为准）

> 总目标（用户定调）：资料库建成「行业、知识面极广、接近全知」的产品经理大脑——帮项目以最好的方式完成需求。PM 的「找 + 判断」沉淀进库 = 把判断力借给 Agent；库用自己筛过的内容替代网上中庸内容。

| # | 阶段 | 内容 | 状态 | 卡点/依赖 |
|---|------|------|------|----------|
| 0 | 选型复审与规划 | 9 项复审 + 第二轮定调（多域网格/路由/零清洗/全量重置），本文档 v2 | ✅ 已完成 | — |
| 1 | 三入口接好并测试 | CLI 直喂 + inbox 目录 + 飞书同步；幂等入库（先删后插/确定性 ID/孤儿清理/hash 跳过）；格式契约 | ✅ 代码+测试完成（871 过 / 1 既有红） | 飞书差用户点一次授权 |
| 2 | 检索层重构 | 双路召回（bm25s ∥ 向量）+ RRF + 质量乘数 | 🔲 待启 | 无（不依赖任何拍板） |
| 3 | 全量重置 + 存量固化 | v1 5300 chunk 推倒重建；旧增量并入存量一起整理固定；补 12 篇 abstract、34 个导航文件按拍板处置 | 🔲 待启 | 待决 ③（导航文件入不入库） |
| 4 | 多域网格 + 路由 | 按域分库（frozen 机制）+ 域判别路由 + 不叠加原则 | 🔲 待启 | 待决 ①②（域划分/路由方式） |
| 5 | 信源梯度外扩 | 库内存量→增量→外部信源→联网搜索的降级链 | 🔲 待启 | 依赖阶段 4 |

**待决事项**：①起步域划分（暂放，与需求要素元数据字段一起议）②路由=术语映射表+需求要素元数据过滤（已定方向，字段清单待议）③34 个导航文件（默认不入库，可推翻）④contextual header 已定案（[领域]>[章节]>[条目] 路径前缀）⑤git 提交切分（待用户示意）。检索默认定案与切片定案见选型笔记「第三轮定调」。

**入口速查（Pi Agent 提示词同步这份）**：
```
python scripts/rag_ingest.py --incremental --file <path>            # 单文档（frontmatter 契约）
python scripts/rag_ingest.py --incremental --text "..." --title "…"  # 手输直喂（自动包壳）
python scripts/rag_ingest.py --inbox domain_kb/source/inbox          # 目录投递（扫描入库）
python scripts/rag_ingest.py --from-feishu --space <id> [--node <id>]  # 飞书拉取（需授权）
python scripts/feishu_auth.py                                        # 飞书授权（浏览器点同意）
```
默认 inbox：`domain_kb/source/inbox/`；状态表：`domain_kb/state/ingest_state.json`；格式契约必填字段：title / source / type / updated（缺项会报缺项清单）。

---

## 一、背景与目标

### 1.1 背景

- 项目已有 PM 业务档案库（纯关键词检索，KBStore/KBWriter），与领域知识库底层独立、上层统一入口。
- 领域知识库 v1 骨架已落地：`src/kb/rag/`（embeddings/splitter/ingest/store）+ `scripts/rag_ingest.py`、`rag_query.py` + `tests/test_rag_*`，语料 5300 chunk（概念层 + 精选论文层）。
- **产品定调更新（v2 起点）**：知识库由两部分组成——
  - **增量部分**：用户自己输入的内容 + 从信息源引入的文档。规定文档大致格式；实时更新、可立即检索；切片语义自包含，新增切片不破坏已有切片库。
  - **存量部分**：大量文献、社区关于选型的讨论、模型信息。可提前批量制备，按最适合该类资料的方式切片，再进混合检索。

### 1.2 目标（本期四阶段）

1. ✅ 检索层重构：双路召回（bm25s 词法 ∥ ChromaDB 向量）+ RRF 融合（修复 v1「词法独中必漏」缺陷）
2. ✅ 增量入库：文档格式契约 + 实时幂等入库（按源先删后插、确定性 ID、孤儿清理、hash 跳过）+ **三个入口全接**（CLI 直喂 / inbox 目录投递 / 飞书输入同步），接口测试通过
3. ✅ 存量类型化切片：论文双索引（abstract 节点 + 章节块）、讨论帖 parent-child
4. ~~模型信息结构化通道~~ **已否决（2026-10-15 用户拍板）**：模型信息不进资料库，临时查询即可

### 1.3 范围（本期不做）

- reranker 精排（留触发条件与候选型号，见 §5.5）
- 查询扩展（multi-query / HyDE）
- 飞书知识地图集成与 feishu_ref 加权实测（保留字段与接口）
- 信源自动引入（arXiv 周更等）的定时调度（增量通道先支持手动/命令实时入库）
- RRF k 的标注扫参定档（本期用 k=60 默认值，扫参在阶段一过线判据里留记录）

---

## 二、语料框架：增量/存量双域

### 2.1 双域结构

| 域 | 子类 | 来源 | 处理时机 | 更新节奏 |
|----|------|------|---------|---------|
| **增量 increment** | 用户输入 | 手输/粘贴 | 实时，进即检索 | 随时 |
| **增量 increment** | 信源引入 | 信息源文档（含后续 arXiv 追踪） | 实时/准实时 | 随时 |
| **存量 stock** | 文献解读 | 人写深度文档（原 concept 层 209 篇） | 批量预处理 | 季度检查 |
| **存量 stock** | 论文 | 论文元数据 + abstract（原 curated 层 514 篇）+ 未来全文 | 批量预处理 | 月度 diff |
| **存量 stock** | 社区讨论帖 | 论坛/知乎/GitHub discussion/HN 选型讨论 | 批量预处理 | 月度补充 |
| **存量 stock** | 模型信息 | 模型卡/参数/价格/能力边界 | 批量 + 结构化 | 随版本 |

质量标记 `quality` 与域解耦，取值 `user_input` / `curated` / `auto`（替代 v1 的 layer 权重 + curated bool），参与检索加权。

**存量更新动作（2026-10-15 五轮定调）**：存量不是冻结不改——更新 = ①对需要切片的自然语言内容做**整理**（源层修订/归并/补缺，落 `domain_kb/source/`）→ ②**重新切片入库**（蓝绿重建，旧库归档不删）；频率低于增量（季/月批量 vs 随时）。

### 2.2 旧三层 → 新双域映射

| v1 | v2 | 处理 |
|----|----|------|
| concept 层 | stock / 文献解读 | 质量标记 curated，切片沿用结构切片 |
| curated-paper 层 | stock / 论文 | 质量标记 curated，abstract 节点 + 章节块 |
| tracked-paper 层（未实现） | increment / 信源引入 | 并入增量通道，不再单独成层 |
| （无） | increment / 用户输入 | 新增 |
| （无） | stock / 讨论帖 | 新增 |
| （无） | stock / 模型信息 | 新增（结构化） |

### 2.3 统一分类

沿用 v1 的 ~25 个统一 `category`（concepts / planning / memory / tool-use / multi-agent / evaluation / frameworks / …），映射表见 `references/RAG知识库_三层分类映射表.md`（分类体系不变，仅层概念取消）。

---

## 三、技术选型确认（v2）

| 组件 | 选型 | 理由 | 复审出处 |
|------|------|------|---------|
| 向量数据库 | ChromaDB（嵌入式，保留） | 规模匹配、零运维、metadata 过滤够用 | 复审八 |
| 嵌入模型 | 硅基流动 `Qwen/Qwen3-Embedding-8B`（4096 维，保留） | 已定稿、真机实测通过；其 last-token pooling 特性同时排除了 late chunking | 复审二 |
| 词法索引 | **bm25s + jieba**（新增） | 纯 Python、快、中文可分词、免重依赖 | 复审八 |
| 融合 | **RRF（k=60 起步）**（推翻 v1 加权融合） | 免归一化、单参数、业界默认 | 复审九 |
| 切片 | 结构优先 + 自包含前缀 + 类型化 | 单文档纯函数 → 增量不重切全库 | 复审二/五/六 |
| 增量入库 | 按源先删后插 + 确定性 ID + hash 跳过 | 幂等、可定向重切 | 复审四 |
| 模型信息 | SQLite 结构化为准 + 向量辅助 | 事实查询只有结构化做得了 | 复审七 |
| 重排 reranker | 跨库合并去重后统一重排，模型可插拔（bge-reranker-v2-m3 / Qwen3-Reranker / 托管 API 按效果定） | 产品视角：不以单机硬件/成本为约束（2026-10-15 修正） | 三轮定调 |

---

## 四、架构设计

### 4.1 模块划分

```
src/kb/rag/
├── __init__.py
├── embeddings.py      # 不变：硅基流动 API 封装（批量 32、重试、4096 维）
├── splitter.py        # 升级：结构优先切片（保留 v1 保护逻辑）+ 类型化切片
│                        · generic/note/paper_abstract/paper_section/thread
│                        · 每 chunk 写 heading_path 前缀（自包含）
├── ingest.py          # 升级：幂等入库（先删后插 + 孤儿清理 + content_hash 跳过）
│                        · 单一入口 ingest_document(doc) 覆盖增量/存量
├── lexical.py         # 🆕 bm25s 词法索引：jieba 分词、全量重建（本规模毫秒级）、
│                        · 与 Chroma 同步维护（每批入库后重建词法索引）
├── store.py           # 重构：双路召回 + RRF + 质量乘数 + 同源去重
└── feishu_sync.py     # 升级：飞书知识库拉取 → 增量入库（真实现，替换占位）

scripts/
├── rag_ingest.py      # 增强：--doc-type / --incremental / 入口形态见待拍板项 1
└── rag_query.py       # 增强：显示融合明细（各路排名 + RRF 分 + 乘数）
```

### 4.2 数据目录

```
domain_kb/
├── chroma/            # 向量（不变）
├── lexical/           # 🆕 bm25s 索引文件
├── sqlite/            # 🆕 模型信息等结构化库（阶段四）
├── state/             # 🆕 源级状态表（content_hash / strategy_version / chunk id 集合）
└── source/
    ├── increment/     # 增量域原文（用户输入 + 信源引入）
    ├── stock/
    │   ├── docs/      # 文献解读（原 concept 层）
    │   ├── papers/    # 论文（原 curated 层 + 未来全文）
    │   ├── threads/   # 社区讨论帖
    │   └── models/    # 模型卡源数据（结构化导入的原料）
    └── inbox/         # 待入库暂存（入口形态待拍板，见 §7）
```

### 4.3 统一 metadata schema（增量/存量共用）

| 字段 | 类型 | 说明 |
|------|------|------|
| `doc_id` | str | 文档唯一 ID（由 source 派生，稳定） |
| `domain` | str | `increment` / `stock` |
| `doc_type` | str | `note` / `interpretation` / `paper` / `thread` / `model_card` |
| `chunk_type` | str | `section` / `abstract` / `op` / `reply` / `thread_parent` / `model_desc` |
| `parent_id` | str\|null | 父块 ID（章节块→abstract 节点；楼→thread parent） |
| `category` | str | 统一分类（沿用映射表） |
| `source` | str | 来源（文件路径 / URL / arxiv_id），全局唯一来源键 |
| `title` / `section_title` / `heading_path` | str | 自包含前缀用 |
| `quality` | str | `user_input` / `curated` / `auto` |
| `feishu_ref` | str\|null | 飞书知识点 ID（保留） |
| `content_hash` | str | 文档内容 hash（幂等跳过依据） |
| `strategy_version` | str | 切片策略版本（v2 起 = `v2`；替代 v1 chunking_version） |
| `chunk_index` | int | 文档内序号 |
| `thread_score` / `stance` | 讨论帖专用 | 回复得分 / 支持·反对·中立 |
| `created_at` / `updated_at` | str | 时间（讨论帖时效过滤用） |
| `is_stale` | bool | 软删标记（策略迁移期间新旧并存） |

**Chunk ID 规则（确定性，幂等重跑不重复）**：

```
通用块：   {doc_id}:{strategy_version}:{chunk_index}
论文摘要： {doc_id}:{strategy_version}:abstract
帖父块：   {doc_id}:{strategy_version}:thread
```

全库重建的触发：`strategy_version` 变更、嵌入模型/维度变更、**或存量的周期性整理→重切**（2026-10-15 五轮定调，扩掉原「唯一触发」口径）。重建一律蓝绿（新库建好再切换），旧库归档不删。

### 4.4 检索流程（v2）

```
query
  │
  ├── 双路召回（同一 where 过滤下并行）
  │     ├── bm25s 全库词法召回 → 候选 A（top N，默认 50）
  │     └── ChromaDB 向量召回 → 候选 B（top N，默认 50）
  │
  ├── RRF 融合：score = Σ 1/(k + rank_i)，k=60（config 可调）
  │
  ├── 质量乘数：× quality_weight（user_input 1.5 / curated 1.2 / auto 1.0）
  │     × feishu_ref_bonus 1.3（命中用户飞书标注时）
  │
  ├── 过滤：is_stale 软删 → 同源去重（max_per_source，默认 1）→ 阈值
  │
  └── Top-K 返回（附各路 rank 与融合明细，供调参）
```

与 v1 的差异：删掉「向量先捞 top_k×5 → 候选内关键词打分」与「0.4/0.6 归一化加权」；关键词通道从「候选内 boost」升为「全库独立召回」。

### 4.5 多域库网格与路由（2026-10-15 第二轮定调，网格阶段实施）

> 总目标（用户定调）：资料库建成「行业、知识面极广、接近全知」的产品经理大脑，帮项目以最好的方式完成需求。覆盖面泛化：AI 范式/模型只是其一，代码方式、泛化方法、硬件（单片机）、IT/PC 等都进网格。PM 的「找 + 判断」沉淀进库 = 把判断力借给 Agent。

- **库布局**：存量按领域分库——每域一个独立 collection（Chroma 同进程多 collection）或独立 chroma 目录。起步域清单（随网格扩展）：`ai`（AI 范式/模型/Agent）、`code`（代码与工程方式）、`methods`（泛化方法论）、`hardware`（单片机等硬件）、`it-pc`（IT/PC 常识）。域库带 `frozen` 标记：单域总结完毕即固定**索引形态**——但库本体不禁止改动，可低频「整理→重切」（五轮定调，frozen 语义收窄）。
- **路由机制**：查询 → 域判别（域关键词/描述表起步，后续可 LLM 分类）→ 目标域库内检索 → 返回；目标域无果 → 逐级降级：其他域库 → 信源梯度外扩（外部信源 → 联网搜索）。飞书知识地图继续承担「人读导航 + 域定位」。
- **不叠加原则（原义保留，五轮定调精确化）**：新内容不往已建索引上**硬叠切片**（向量切片无法完善叠加进已建成的库）——进语料层（`domain_kb/source/`），随下一次存量「整理→重切」成建制入库；跨域问题由路由到多个域库分别取片再合并，不追求单个向量空间装下全部领域。
- **准入即合规（库侧零清洗）**：格式契约 = 准入标准；清洗在入库前完成（入口侧/上游 Agent 按规则执行），库内不做任何清洗。旧增量并入存量一次性整理固化（当前旧增量为 0——入口尚未上线，重建时直接按新规则固化即可）。
- **全量重置**：v1 的 5300 chunk 不做迁移兼容，strategy_version 升 v2 蓝绿重建，重建过程即一次性整理固化。缺口清单：`D:\AgentHub\Hermes\output\AI-PM-Agent-RAG\存量重置缺口清单.md`（实情：34 篇「缺 frontmatter」全部是各分类 `index.md` 与 `graph/ingest-log/log` 类导航文件，175 篇正文文档全部自带 frontmatter；12 篇论文缺 abstract）。
- **与进行中工作的兼容**：三入口（CLI/inbox/飞书）先把增量数据收进增量库（单 collection 落地），入口接口不因网格化改变；网格阶段只改「写哪个 collection + 检索怎么路由」。

---

## 五、核心模块设计

### 5.1 embeddings.py（不变）

沿用 v1：硅基流动 API、`Qwen/Qwen3-Embedding-8B`、4096 维、批量 32、重试 2 次、`SILICONFLOW_API_KEY`。

### 5.2 splitter.py（升级）

```python
def split_document(text: str, doc_type: str, metadata: dict) -> list[dict]
```

| doc_type | 切片方式 | 粒度 |
|----------|---------|------|
| `note` / `interpretation` / `generic` | 结构切片：1-3 级标题块 → 超 800 字按段落递归兜底（v1 逻辑保留：行边界重叠、表格保护、空壳块剔除） | 500-800 字 |
| `paper`（仅 abstract） | 整篇一块（v1 不变） | 整篇 |
| `paper`（有全文） | abstract 摘要节点 + 章节 section chunk 双索引 | 摘要 1 块/篇；章节 300-600 字 |
| `thread` | thread parent（OP + 高赞/采纳回复聚合）+ 楼级子块 | parent 1-4k token；回复 200-500 字 |

**自包含处理（所有类型，2026-10-15 三轮定案）**：每 chunk 内容前缀拼 Contextual Header `[领域] > [章节] > [条目]`（参与嵌入与词法索引，不只是 metadata）；`thread` 子块前缀拼 `Re: {parent 标题}`。
**断点优先级（硬约束）：标题 > 段落 > 约束词 > 字数上限**——宁可块只有 150 字，也不在条件句与结论之间切断（约束词表：必须/不得/仅当/前提/假设/适用/不适用/除非…，规则实现零成本）。结构化内容 overlap=0，仅叙事长文约 10%。
**父子块必须上**：子块参与检索、父块进上下文；父块/摘要块承接宽泛提问（进索引但排在子块之后）。

### 5.3 ingest.py（升级为幂等增量入库）

```python
class RAGIngest:
    def ingest_document(self, text: str, doc_type: str, source: str, **meta) -> dict
        # 单文档入库（增量实时入口）：解析 frontmatter → 分类 → 切片 → 嵌入
        # → 按 source 先删后插 → 孤儿清理 → 更新 state → 重建词法索引
    def ingest_dir(self, dir_path: str, doc_type: str) -> int   # 存量批量
    def skip_if_unchanged(self, source: str, content_hash: str) -> bool
    def delete_by_source(self, source: str) -> int               # v1 保留
    def delete_by_layer(...)  → 改为 delete_by_domain(domain: str)
```

入库流程：
```
归档原文到 domain_kb/source/（三入口统一，先归档后切片——2026-10-15 四轮硬规矩）
     → frontmatter 解析（缺省自动包壳：source=内容hash、type=note）
     → content_hash + strategy_version 比 state 表，均未变则跳过
     → 切片（heading_path 前缀）→ 批量嵌入 → Chroma upsert
     → 孤儿清理（本源旧 chunk id 集合 − 新集合 → delete）
     → 重建 bm25s 词法索引 → 写 state 表
```

### 5.4 lexical.py（新增）

- bm25s 全量索引，jieba 分词（英文数字原样保留 token；型号/术语加自定义词典 `domain_kb/lexical/userdict.txt`）。
- 索引 = 全库 chunk 的纯函数，每批入库后整体重建（几万 chunk 毫秒级，不需要增量维护 BM25）。
- 检索接口：`search(query, n) -> [(chunk_id, bm25_score, rank)]`。

### 5.5 store.py（重构）

```python
class RAGStore:
    def search(self, query, top_k=5, domain=None, doc_types=None,
               category=None, feishu_refs=None) -> list[dict]
```

- 双路召回（各 top N=50，config 可调）→ RRF（k=60）→ 质量乘数 → 过滤去重 → Top-K。
- 返回项保留 v1 字段，新增 `bm25_rank` / `vector_rank` / `rrf_score` 明细。
- **reranker 挂载点**：`search()` 末尾预留 `rerank(candidates)` 钩子（默认直通）。触发条件与选型见 §三；启用时改 config `reranker.enabled: true`，不改调用方。

### 5.6 模型信息（不入库，2026-10-15 用户拍板）

模型信息（参数/价格/能力边界）不进资料库——写进库里没有太多意义，临时查询即可，也不适合混进资料库语料。模型卡叙述文本如确需参考，走联网/临时检索，不单独建通道。常识约束保留：数值事实不要只依赖向量检索回答。

---

## 六、增量文档格式契约

```yaml
---
title: 必填
source: 必填（URL | 文件路径 | "用户手输"），全局唯一来源键
type: note | doc | snippet
updated: YYYY-MM-DD
tags: [可选]
---
```

- 系统字段（doc_id / content_hash / strategy_version / chunk_index / heading_path）由管道写入，用户不写。
- 结构要求：`#/##/###` 分层；标题下避免超 800 字巨型段落（不强制，有兜底）。
- 手输纯文本：管道自动包壳（source=内容 hash，type=note），零格式负担。
- 校验：frontmatter 缺必填字段 → 入库时报错并给出缺项清单（不静默吞）。

---

## 七、CLI 工具

```bash
# 存量批量（v1 保留，doc_type 语义更新）
python scripts/rag_ingest.py --doc-type interpretation --source domain_kb/source/stock/docs/
python scripts/rag_ingest.py --doc-type paper --source domain_kb/source/stock/papers/papers_with_abstract.json

# 增量单文档实时入库（新增）
python scripts/rag_ingest.py --incremental --file <path>            # 按格式契约入库
python scripts/rag_ingest.py --incremental --text "..." --title "…"  # 手输直喂（自动包壳）

# inbox 目录投递（新增）：扫描目录下新增 .md/.txt 入库，已入过的按 hash 跳过
python scripts/rag_ingest.py --inbox domain_kb/source/inbox/

# 飞书输入同步（新增）：从飞书知识库拉文档入增量库（需用户身份 OAuth，见 §5.3/§10）
python scripts/rag_ingest.py --from-feishu --space <id> [--node <id>]

# 检索（增强：显示双路排名与融合明细）
python scripts/rag_query.py --query "Qwen3-Embedding-8B 的维度" --json
python scripts/rag_query.py --query "Agent 记忆机制" --domain stock --doc-type paper
```

**入口形态（2026-10-15 拍板：三个全接）**：CLI 直喂、inbox 目录投递两个本地入口的地址（命令 + 目录路径）写进 Pi Agent 提示词（`.pi/skills/`），由 Pi 按用户口令执行入库；飞书入口接第三方软件/仓库知识库系统并跑通真实拉取。inbox 的实时性用「Pi 收到口令即跑 `--inbox`」驱动，不设常驻 watcher。

---

## 八、配置变更（config.yaml `domain_kb` 段，v2）

```yaml
domain_kb:
  chroma_path: ./domain_kb/chroma
  lexical_path: ./domain_kb/lexical
  state_path: ./domain_kb/state
  source_path: ./domain_kb/source
  strategy_version: v2

  embedding:                      # 不变
    provider: siliconflow
    model: Qwen/Qwen3-Embedding-8B
    api_key_env: "SILICONFLOW_API_KEY"
    api_base: "https://api.siliconflow.cn/v1"
    dimensions: 4096
    batch_size: 32

  lexical:
    tokenizer: jieba
    user_dict: ./domain_kb/lexical/userdict.txt

  retrieval:
    top_k: 5
    candidate_k: 50               # 双路各召回量
    fusion: rrf
    rrf_k: 60                     # 扫参候选 {10,30,60,100}
    quality_weights: {user_input: 1.5, curated: 1.2, auto: 1.0}
    feishu_ref_bonus: 1.3
    max_per_source: 1

  contextual_header:              # 可选增强（待拍板项 2）
    enabled: true
    scope: stock                  # stock | all | off

  reranker:
    enabled: false                # 触发条件见 §5.5
    model: Qwen/Qwen3-Reranker-0.6B
```

requirements.txt 新增：`bm25s`、`jieba`（阶段四加 `sqlalchemy` 或直接 stdlib `sqlite3`，倾向后者零依赖）。

---

## 九、实施步骤、里程碑与过线判据

> 分期原则：**先修缺陷（检索），再建增量，再类型化存量，最后结构化**。每阶段过线才算完，判据是可执行检查不是描述。

### 阶段一：检索层重构（双路 + RRF）——优先级最高

| 步骤 | 内容 |
|------|------|
| 1 | `lexical.py`（bm25s + jieba + userdict） |
| 2 | `store.py` 重构（双路召回 + RRF + 乘数 + 过滤），保留 v1 接口签名兼容 |
| 3 | 词法索引与 Chroma 的同步维护（ingest 挂重建钩子） |

**过线判据**：
- [ ] 构造 ≥5 条「纯词法命中」查询（精确模型名/型号/人名，向量路查不到的），新检索全部命中，v1 逻辑复现漏检；
- [ ] 既有 `tests/test_rag_*` 适配后全绿；
- [ ] k∈{10,30,60,100} 各跑一遍固定查询集，结果记录进 `domain_kb/update_logs/`（定档用）。

### 阶段二：增量入库（格式契约 + 幂等）

| 步骤 | 内容 |
|------|------|
| 4 | frontmatter 解析 + 自动包壳 + 必填校验 |
| 5 | `ingest_document` 幂等入库（先删后插 + 孤儿清理 + hash 跳过 + state 表） |
| 6 | `--incremental` CLI 入口（入口形态按待拍板项 1） |

**过线判据**：
- [ ] 同一文档入库两次，chunk ID 无重复、条数不变（幂等）；
- [ ] 修改文档只影响该 source 的 chunk，其余 source 的 ID 与向量不动（抽样核对）；
- [ ] 新版块数少于旧版时旧块被清掉（孤儿清理验证）；
- [ ] 内容未变的文档重跑走 skip，无嵌入 API 调用（日志核对）。

### 阶段三：存量类型化切片（论文双索引 + 讨论帖 parent-child）

| 步骤 | 内容 |
|------|------|
| 7 | paper 全文双索引切片（abstract 节点 + 章节块 + heading_path） |
| 8 | thread 切片（parent 组装 + 楼子块 + stance/score 元数据） |
| 9 | contextual header 开关（按待拍板项 2） |

**过线判据**：
- [ ] 「这篇论文讲什么」类查询命中 abstract 节点，「具体方法/结论」类查询命中章节块（各 ≥3 条验证）；
- [ ] 讨论帖查询命中楼级块，且结果带 parent 回填（脉络可读）；
- [ ] v1 的 5300 chunk 全量重置重建（不迁移兼容）后，检索质量不低于 v1（同查询集对比 Top-5 命中）；缺 frontmatter 的 index/log 类文件补 `type: index` 或不入库（重置时定）。

### 阶段四：三入口收尾与跑通（替代原「模型信息结构化」——已否决）

| 步骤 | 内容 |
|------|------|
| 10 | 飞书入口真实现：拉取飞书知识库文档 → 增量入库，探针真调通（spaces 列表 + 单文档拉取 + 回读验证） |
| 11 | 本地两入口地址写入 Pi Agent 提示词（`.pi/skills/`） |

**过线判据**：
- [ ] 飞书：真实拉取 ≥1 篇文档入库，库里回读命中（授权失效则给出授权链接与所需 scope，由用户配合完成后再验）；
- [ ] 本地：CLI 与 inbox 两入口命令在提示词文档中可查、按文档操作即成功入库；
- [ ] 三入口各自的入口测试全绿。

### 里程碑

- **M1**：阶段一过线（词法独中不再漏）→ 检索缺陷修复
- **M2**：阶段二过线（增量实时可检索）
- **M3**：阶段三过线（存量类型化完成，5300 chunk 迁移完毕）
- **M4**：阶段四过线（三入口跑通）→ v2 全量完成

---

## 十、风险与注意事项

1. **strategy_version / 嵌入模型变更 = 全库重建**（蓝绿切换，重建期双份磁盘）。全量约 4000-6500 个嵌入，硅基流动单轮约 ¥0.2，30-60 分钟。
2. **bm25s 与 Chroma 的一致性**：双写顺序固定「先 Chroma 后重建词法索引」，词法索引是全量重建型（毫秒级），不存在半新半旧。
3. **jieba 对英文型号/术语的切分**（如 `Qwen3-Embedding-8B`、`bm25s`）：用 userdict 补词，切分质量直接影响词法召回——阶段一过线判据里的「纯词法命中」查询集就是验这个。
4. **拍板结果（2026-10-15）**：入口三选全接；模型信息不入库（阶段四改为三入口收尾）；切片粒度/contextual header 待讨论（不阻塞阶段二入口实现）。飞书入口依赖用户身份 OAuth（user token 有效期短、refresh_token 一次性需回写轮换），授权失效属正常情况，走用户重新授权即可，不阻塞本地两入口。
5. **挂起冲突 4 条**（语义分块收益、RRF k、contextual vs late、Chroma 稀疏能力）不裁决，处置方式见选型笔记「挂起冲突清单」。
6. **飞书入口原文归档缺口**：拉取文本需先落 `domain_kb/source/increment/` 归档再切片入库（四轮硬规矩），当前实现缺失，随阶段一/二补。
7. **RRF 后乘数的量纲**：乘数直接乘 RRF 分（0-1 区间），量纲合理；若扫参发现乘数被 k 淹没，改为「先乘后融合」的 rank 加权变体（留 TODO，不预先实现）。

---

## 十一、后续阶段（本期不做）

0. **信源梯度外扩路由**（两库产品定位落地）：Agent 取信源按「库内存量 → 库内增量 → 外部信源 → 联网搜索」梯度逐级外扩，检索无果才降级——资料库 = 搜索优化的顶层，增量库 = 偏好注入通道（用户认可的优质资料，对抗联网结果的中位化）
1. reranker 精排实测（触发条件达成时，Qwen3-Reranker-0.6B CPU）
2. 查询扩展（multi-query / HyDE）
3. 飞书知识地图集成（feishu_ref 加权实测 + 结构对齐）
4. `kb_query.py` 统一入口（`--domain` 参数，两套知识库统一查询）
5. 信源自动引入定时调度（arXiv 周更归入增量通道）
6. RRF k 标注扫参定档 + 检索评测集固化（RAGAS 三测：上下文准确性/忠实度/相关性）
7. LanceDB 迁移评估（规模涨到几十万 chunk 或 Chroma 稀疏能力成熟时）

---

## 相关文档

- 《AI_Agent_知识库_RAG选型笔记》—— 决策过程（正文 8 项历史 + 文末 2026-10-15 选型复审 9 项）
- 《RAG知识库_三层分类映射表》—— 分类映射（v2 沿用，层概念取消）
- 《RAG知识库实施指南_加权检索调优篇》—— 结构信号加权思路仍有效，其中 0.4/0.6 融合公式已被 RRF 取代
- 《RAG知识库实施指南_自动更新篇》—— chunk 策略与 ID 设计原则仍有效，三层更新节奏已被增量/存量节奏取代
- 《飞书知识地图_目录结构设计》—— 飞书侧结构（后续阶段）

<!-- 注入签名: 执行者=Hermes | 时间=2026-10-15 | 版本=v2 | 目标=RAG知识库实施规划.md | 状态=已生效 | 改动=v2 重构（增量/存量双域 + 双路召回 RRF + 类型化切片 + 幂等增量入库，四阶段带过线判据）-->
