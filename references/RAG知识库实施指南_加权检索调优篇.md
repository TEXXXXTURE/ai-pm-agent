# RAG 知识库加权检索调优方案

> 适用对象：AI PM Agent 领域知识库（三层结构 + 统一分类 + 领域聚焦）
> 核心思路：我们的知识库规模小、结构清晰、分类明确，不需要复杂的黑盒调参——用结构信息（层/分类/标题）做加权，比纯靠 embedding 语义匹配更准、更可控。
> 性质：调优方案，供 Code 侧实现后按此方案做效果验证和参数迭代

---

## 一、为什么我们的知识库调优空间大

很多 RAG 系统调优难，是因为：
- 内容杂（什么领域都有）
- 结构乱（各种格式混在一起）
- 规模大（几十万条，调一次成本高）

**我们的情况正好相反**：

| 特点 | 对调优的意义 |
|------|-------------|
| 领域聚焦（只有 AI Agent） | 词汇歧义少，"memory" 不会跟计算机内存混淆 |
| 三层结构明确 | 质量差异已知，可以直接用层权重拉开差距 |
| 统一分类体系 | 每个 chunk 都有 category 标签，可以做分类级加权 |
| 概念层是结构化 Markdown | 有标题层级，标题命中的权重可以高于正文 |
| 规模小（几千个 chunk） | 全量测试成本低，可以做 A/B 对比 |
| 中英文混合 | 嵌入模型已选 Doubao，中英文效果都还行 |

**结论**：我们的知识库调优，重点不是"让模型更聪明"，而是**把已知的结构信息用好**——层、分类、标题、精选标记，这些都是白给的信号，不用白不用。

---

## 二、调优总原则

### 原则 1：结构信号 > 语义信号

能靠 metadata（层/分类/标题）解决的，就不指望 embedding 猜。

比如用户问"Agent 有哪些分类？"——
- 好的做法：先看 `category=concepts` 的 chunk，层权重 ×2.0，标题命中再加权
- 差的做法：纯靠向量相似度，可能把论文里提到分类的句子也捞上来

### 原则 2：权重分三层，从粗到细

```
第一层：层权重（粗粒度，概念 > 精选 > 追踪）
    ↓
第二层：分类权重（中粒度，查询命中哪个分类就加权）
    ↓
第三层：字段权重（细粒度，标题命中 > 章节标题 > 正文）
```

### 原则 3：默认值够用，再微调

先给一套合理的默认权重，跑通基本功能。然后用测试集验证，再针对具体问题调。不要一上来就精细调参。

### 原则 4：所有权重可配置，不改代码

权重全部放 config.yaml 里，调参只改配置、不改代码。方便做 A/B 测试。

---

## 三、完整加权公式

### 3.1 公式总览

```
最终得分 = 基础融合分 × 层权重 × 分类匹配加成 × 标题命中加成 × 精选加成 × 飞书引用加成
```

其中：
- **基础融合分** = 0.4 × 关键词归一化分 + 0.6 × 向量相似度（0~1 之间）
- 后面各项都是乘数（≥1.0 表示加分，=1.0 表示不加不减）

### 3.2 第一层：层权重（layer_weight）

| 层 | 默认权重 | 为什么 |
|----|---------|--------|
| concept（概念解读层） | 2.0 | 人写的、结构化的、质量最高、最适合回答"是什么/为什么/怎么分类" |
| curated-paper（精选论文层） | 1.5 | 人工精选过，质量有保证，但偏论文视角，适合溯源和深入 |
| tracked-paper（新论文追踪层） | 1.0 | 自动过滤的，质量参差，作为补充 |

**调参建议**：
- 如果发现概念层内容太泛、论文层更精准 → 把 concept 降到 1.5，curated 升到 1.8
- 如果发现追踪层噪音太多 → 把 tracked 降到 0.8（低于 1.0 就是惩罚）
- 初期用默认值就行，等有了测试集再调

### 3.3 第二层：分类匹配加成（category_boost）

**核心思路**：先判断用户的查询属于哪个分类，然后给该分类的 chunk 加权。

**怎么判断查询属于哪个分类**：
- 方法一（简单版，首期实现）：关键词匹配分类名和分类同义词
  - 比如查询里有"规划/plan" → `planning` 分类加权
  - 查询里有"记忆/memory" → `memory` 分类加权
  - 查询里有"工具/tool/function call" → `tool-use` 分类加权
- 方法二（进阶版，后续加）：用 embedding 算查询和各分类描述的相似度，取 Top-1 或 Top-2

**加成幅度**：
- 精确匹配分类名 → ×1.5
- 匹配分类同义词 → ×1.3
- 匹配多个分类 → 取最高的那个，不累乘

**分类关键词表**（首期用这个，后续可扩展）：

| 分类 | 关键词 |
|------|--------|
| concepts | 概念、定义、分类、基础、入门、overview、introduction |
| planning | 规划、计划、plan、planning、ReAct、思考、推理链 |
| memory | 记忆、memory、记忆机制、长期记忆、短期记忆 |
| tool-use | 工具、tool、function call、函数调用、工具使用 |
| multi-agent | 多智能体、多 agent、multi-agent、协作、通信 |
| evaluation | 评估、评测、evaluation、benchmark、测评 |
| frameworks | 框架、framework、LangChain、LangGraph、AutoGen |
| platforms | 平台、platform、MCP、agent 平台 |
| coding | 代码、编程、coding、代码生成、SWE |
| research | 研究、research、科研、论文 |
| safety | 安全、safety、风险、对齐、安全合规 |
| rag | RAG、检索、知识库、向量检索 |

**实现方式**：
- 代码里维护一个 dict：`category_keywords = {"planning": ["规划", "plan", "planning", "ReAct"], ...}`
- 查询进来后，遍历这个 dict，看查询里包含哪些关键词
- 命中的分类，给对应的 chunk 加分类匹配加成
- 没命中任何分类 → 不加权（×1.0），全靠语义

### 3.4 第三层：标题命中加成（title_boost）

**核心思路**：用户查询的关键词，如果出现在文档标题或章节标题里，说明这个 chunk 更相关，加权。

**两级加权**：
- 文档标题命中（`title` 字段包含关键词）→ ×1.8
- 章节标题命中（`section_title` 字段包含关键词）→ ×1.4
- 都命中 → 取高的那个，不累乘
- 都没命中 → ×1.0

**为什么标题权重这么高**：
- 在结构化文档里，标题 = 这个 chunk 的主题概括
- 概念层的 Markdown 文档，标题就是内容的精准索引
- 标题命中比正文命中的相关性强得多

**实现注意**：
- 用简单的字符串包含匹配就行，不需要 embedding
- 关键词是查询的分词结果，不是整个查询串
- 中文分词用 jieba 或简单的 n-gram 都行，首期可以用简单的关键词列表匹配

### 3.5 其他加成项

| 加成项 | 默认值 | 说明 |
|--------|--------|------|
| curated_bonus | 1.2 | 人工精选的内容额外加成（精选论文层默认有，追踪层没有） |
| feishu_ref_bonus | 1.3 | 有对应飞书知识点的 chunk 加权（飞书知识地图建好后生效） |

---

## 四、针对 AI PM Agent 的特殊调优

我们这个 RAG 库不是给普通用户查资料用的，是给 AI PM Agent 工作流里的节点用的。使用场景有特点，调优也要跟着适配。

### 4.1 查询感知动态权重

**核心思路**：不同的节点查询知识库的目的不一样，权重应该不一样。

比如：
- **探索节点**（竞品调研、市场分析）→ 更需要最新论文和框架信息 → 追踪层权重可以提高
- **PRD 节点**（写 PRD 需要概念支撑）→ 更需要清晰的定义和分类 → 概念层权重最高
- **模型选型节点**（bake-off 前查档案）→ 更需要评测论文和基准 → evaluation 分类加权

**实现方式**：
- `RAGStore.search()` 增加一个 `query_type` 参数
- 不同的 query_type 对应不同的权重预设
- 业务节点调用检索时，传入自己的 query_type

**权重预设示例**：

| query_type | concept 权重 | curated 权重 | tracked 权重 | 分类加权重点 |
|-----------|-------------|-------------|-------------|------------|
| `concept_explain`（概念解释） | 2.5 | 1.2 | 0.8 | concepts 类 ×1.5 |
| `paper_research`（论文调研） | 1.2 | 2.0 | 1.5 | 对应分类 ×1.5 |
| `framework_compare`（框架对比） | 1.5 | 1.5 | 1.2 | frameworks 类 ×2.0 |
| `eval_benchmark`（评测基准） | 1.0 | 2.0 | 1.8 | evaluation / benchmarks ×2.0 |
| `default`（通用） | 2.0 | 1.5 | 1.0 | 自动分类匹配 |

**首期实现建议**：先只做 `default` 模式，等工作流里的节点实际调用 RAG 了，再根据各节点的反馈加预设。

### 4.2 飞书知识地图联动加权

飞书知识地图建好后，每个飞书知识点对应 RAG 里的一组 chunk。飞书那边有人工整理的目录结构和知识点描述，质量很高。

**怎么用**：
- 用户在飞书里点某个知识点 → 这个知识点关联的所有 chunk 都加 `feishu_ref_bonus`（×1.3）
- 相当于"飞书人工精选过的内容，再额外加权"
- 这是一种**人机协同**的调优方式——人在飞书那边整理知识结构，RAG 检索时利用这个结构信号

### 4.3 少用 reranker，用好结构信息

很多 RAG 系统喜欢加 reranker（重排序模型），说能提升效果。但我们的情况：

- 规模小（几千 chunk），检索返回的候选集本来就不大
- 结构信息丰富（层/分类/标题），这些信息 reranker 不一定能利用
- reranker 增加延迟和成本

**结论**：**先不急于加 reranker**，把结构加权做足了，效果应该够用。等以后规模大了、结构信号用满了，再加 reranker 不迟。

---

## 五、调参方法与验证

### 5.1 测试集怎么建

调优需要有测试集，不然就是瞎调。

**测试集格式**：
```json
[
  {
    "query": "Agent 有哪些主要的设计模式？",
    "expected_categories": ["concepts", "architecture"],
    "expected_layers": ["concept"],
    "must_contain": ["Reflection", "Tool Use", "Planning", "Multi-Agent"],
    "ideal_top3_sources": [
      "concept/Concepts/agentic-ai-design-patterns.md",
      "concept/Architecture/workflow-vs-agent.md",
      "concept/Concepts/agent-taxonomy.md"
    ]
  },
  ...
]
```

**测试集从哪来**：
1. 首期人工写 20-30 条，覆盖主要分类和常见查询类型
2. 后续从实际使用中收集 bad case，追加到测试集
3. 每次调参后跑一遍测试集，看指标有没有提升

### 5.2 评估指标

因为是结构化知识库，我们的评估不追求"标准答案"，而是看**检索结果的质量和相关性**。

| 指标 | 怎么算 | 目标 |
|------|--------|------|
| Top-1 分类准确率 | 查询的正确分类是否在结果第 1 条里 | ≥80% |
| Top-3 层准确率 | 正确的层是否在前 3 条里占多数 | ≥90% |
| 关键词命中率 | 结果里包含查询关键词的比例 | ≥95% |
| 概念层前置率 | 概念层结果在前 5 条里的占比 | ≥60%（默认权重下） |
| 人工评分（抽样） | 1-5 分评价相关性 | 平均分 ≥4.0 |

**首期不需要全做**，先有 20 条测试集 + Top-1 分类准确率 + 人工抽样评分，就能指导调参了。

### 5.3 调参步骤

```
第一步：用默认权重跑测试集，记录基线分数
        ↓
第二步：调层权重（2.0/1.5/1.0 → 试几组组合）
        找到最优的层权重组合
        ↓
第三步：加分类匹配加成
        验证分类加权有没有提升准确率
        ↓
第四步：加标题命中加成
        验证标题加权有没有提升精准度
        ↓
第五步：固定最优参数，写入 config.yaml 默认值
```

**注意**：
- 每次只调一个变量，不要同时改多个权重——不然不知道是哪个起了作用
- 调完一组就跑测试集，记录分数
- 不要过拟合测试集——测试集只有 20 条时，别调到 100% 准确，差不多就行

### 5.4 常见问题与调参方向

| 问题现象 | 可能原因 | 调参方向 |
|---------|---------|---------|
| 返回的都是论文，概念解释太少 | concept 层权重不够 | 把 concept 权重从 2.0 调到 2.5 |
| 返回的内容分类不对 | 分类匹配没命中 or 分类加成不够 | 加分类关键词 / 把 category_boost 从 1.5 调到 2.0 |
| 返回的都是同一个文档的不同 chunk | 标题命中加成太高 or 文档太长 chunk 多 | 降低 title_boost / 加"同文档去重"逻辑（每个文档最多 2 条进 Top-K） |
| 追踪层噪音太多 | tracked 权重太高 | 把 tracked 从 1.0 降到 0.7-0.8 |
| 查询太泛，结果不精准 | 关键词太少，分类匹配不准 | 增加查询理解的逻辑（比如先判断查询类型） |

---

## 六、代码实现要点

### 6.1 配置结构（config.yaml）

```yaml
domain_kb:
  retrieval:
    top_k: 5
    keyword_weight: 0.4
    vector_weight: 0.6

    # 层权重
    layer_weights:
      concept: 2.0
      curated-paper: 1.5
      tracked-paper: 1.0

    # 分类匹配加成
    category_boost:
      enabled: true
      boost: 1.5            # 精确匹配分类名
      synonym_boost: 1.3    # 匹配同义词
      # 分类关键词表（简化版，完整的放代码里或单独文件）
      keywords:
        planning: ["规划", "plan", "planning", "ReAct"]
        memory: ["记忆", "memory"]
        # ... 其他分类

    # 标题命中加成
    title_boost:
      enabled: true
      title_boost: 1.8      # 文档标题命中
      section_boost: 1.4    # 章节标题命中

    # 其他加成
    curated_bonus: 1.2
    feishu_ref_bonus: 1.3

    # 查询类型预设（后续扩展）
    presets:
      concept_explain:
        layer_weights: {concept: 2.5, curated-paper: 1.2, tracked-paper: 0.8}
      paper_research:
        layer_weights: {concept: 1.2, curated-paper: 2.0, tracked-paper: 1.5}
```

### 6.2 检索器中的加权逻辑

```python
def _compute_final_score(self, chunk: dict, query: str, preset: str = "default") -> float:
    """计算最终加权得分"""
    # 1. 基础融合分
    base_score = (
        self.config.keyword_weight * chunk["keyword_score"]
        + self.config.vector_weight * chunk["vector_score"]
    )

    # 2. 层权重
    layer_weight = self.config.layer_weights.get(chunk["layer"], 1.0)
    score = base_score * layer_weight

    # 3. 分类匹配加成
    if self.config.category_boost.enabled:
        matched_categories = self._match_categories(query)
        if chunk["category"] in matched_categories:
            score *= self.config.category_boost.boost

    # 4. 标题命中加成
    if self.config.title_boost.enabled:
        if self._query_in_title(query, chunk.get("title", "")):
            score *= self.config.title_boost.title_boost
        elif self._query_in_section(query, chunk.get("section_title", "")):
            score *= self.config.title_boost.section_boost

    # 5. 精选加成
    if chunk.get("curated"):
        score *= self.config.curated_bonus

    # 6. 飞书引用加成
    if chunk.get("feishu_ref"):
        score *= self.config.feishu_ref_bonus

    return score
```

### 6.3 首期 MVP 做哪些

调优功能不需要一次全做，按优先级来：

| 优先级 | 功能 | 为什么 |
|--------|------|--------|
| P0 | 层权重 | 最基础、效果最明显，几行代码就搞定 |
| P0 | 精选加成 | 跟层权重一起的，直接读 metadata 就行 |
| P1 | 标题命中加成 | 利用结构化文档优势，提升精准度 |
| P1 | 分类匹配加成（关键词版） | 中等成本，效果明显 |
| P2 | 查询类型预设 | 等有实际调用场景了再加 |
| P2 | 测试集 + 评估脚本 | 等功能稳定了再建测试集系统化调优 |
| P3 | 飞书联动加权 | 飞书知识地图建好了再加 |
| P3 | reranker | 规模大了、结构信号用满了再加 |

**首期（RAG 骨架阶段）做 P0 + P1**，就已经比纯向量检索强很多了。

---

## 七、总结

我们这个知识库的调优，核心就一句话：

> **用足结构信息，不跟 embedding 死磕。**

三层结构是白给的质量信号，分类标签是白给的主题信号，标题层级是白给的重要性信号——把这些都加权用上，效果就不会差。

而且因为规模小、结构清晰，我们可以：
- 权重全透明、可解释（知道为什么这条排前面）
- 调参成本低（改个配置就生效）
- 效果可预期（加了什么权重、影响哪些查询，都能说清楚）

这比黑盒的 reranker 更适合我们的场景——**知识型检索，精准和可控比"看起来聪明"更重要。**

---

## 相关文档

- 《RAG 知识库实施规划》—— 整体架构和实施步骤
- 《RAG 知识库实施指南_自动更新篇》—— 持续更新机制
- 《RAG 知识库_三层分类映射表》—— 统一分类体系

<!-- [W02 2026-09-12] RAG 加权检索调优方案 -->
