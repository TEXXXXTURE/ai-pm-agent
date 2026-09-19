# RAG 知识库三层分类映射表

> 本文档定义三层知识库的分类对齐关系，以及飞书知识地图的统一分类体系。
> 解决的问题：概念层有31个分类、精选论文层有10个分类、追踪层用arXiv原生分类，三套体系颗粒度和视角都不同，需要统一映射。

---

## 一、顶层设计原则

### 1.1 飞书知识地图的分类是"主分类"

飞书知识地图的 5 大类是整个系统的顶层分类框架，所有知识层都向它对齐：

```
一、基础概念与分类
二、架构与核心模式
三、框架与平台
四、应用与场景
五、项目内部文档
```

RAG 库中每个 chunk 的 `category` 字段使用的是**飞书知识地图的二级分类**（如 `planning`、`memory`、`tool-use`），这样 Agent 从飞书地图拿到 category 后，可以直接在 RAG 库中做定向检索。

### 1.2 各层分类映射到飞书二级分类

不是每个层都有完整的 5 大类内容：
- 概念层偏工程实践，覆盖广但颗粒度不同
- 精选论文层偏学术研究，按功能模块分
- 追踪层最粗，用 arXiv 原生分类

**映射原则**：
- 多对一：多个细分类映射到同一个统一分类
- 粒度统一：统一分类的粒度是"飞书二级标题"的粒度
- 不丢失信息：原始分类保存在 chunk metadata 的 `source_category` 字段中

---

## 二、统一分类体系（飞书二级 → 三级）

| 飞书一级 | 飞书二级（统一 category） | 说明 |
|---------|------------------------|------|
| 一、基础概念与分类 | `concepts` | Agent 定义、分类体系、自主程度轴、范式演进 |
| 一、基础概念与分类 | `surveys` | 综述论文、立场论文 |
| 二、架构与核心模式 | `planning` | 规划与推理 |
| 二、架构与核心模式 | `memory` | 记忆系统 |
| 二、架构与核心模式 | `tool-use` | 工具使用 |
| 二、架构与核心模式 | `multi-agent` | 多 Agent 协作 |
| 二、架构与核心模式 | `evaluation` | 评估与自检 |
| 二、架构与核心模式 | `architecture` | 架构设计、参考架构、设计模式 |
| 三、框架与平台 | `frameworks` | LangChain / LangGraph / AutoGen / CrewAI 等 |
| 三、框架与平台 | `platforms` | Agent 平台、MCP、Agent 协议 |
| 三、框架与平台 | `harness` | Agent Harness / 运行时基础设施 |
| 四、应用与场景 | `coding` | 代码助手 |
| 四、应用与场景 | `research` | 研究助手 |
| 四、应用与场景 | `product-pm` | 产品/项目经理助手 |
| 四、应用与场景 | `environments` | 交互环境（Web / 代码 / 具身） |
| 四、应用与场景 | `applications-other` | 其他应用场景 |
| 跨领域 | `safety` | 安全与对齐 |
| 跨领域 | `observability` | 可观测性与运维 |
| 跨领域 | `production` | 生产最佳实践 |
| 跨领域 | `standards` | 行业标准与协议 |
| 跨领域 | `context-engineering` | 上下文工程 |
| 跨领域 | `prompt-engineering` | 提示工程 |
| 跨领域 | `rag` | 检索增强生成 |
| 跨领域 | `benchmarks` | 基准测试 |
| 跨领域 | `maturity-models` | 成熟度模型 |
| 厂商专区 | `vendor-anthropic` | Anthropic 相关 |
| 厂商专区 | `vendor-openai` | OpenAI 相关 |
| 厂商专区 | `vendor-google` | Google 相关 |
| 厂商专区 | `vendor-microsoft` | Microsoft 相关 |
| 厂商专区 | `vendor-aws` | AWS 相关 |

> 注：厂商专区是概念层特有的内容，论文层基本没有对应，单独放一个分类。

---

## 三、概念层 → 统一分类映射

概念层有 31 个目录，映射到统一分类如下：

| 概念层目录 | 统一 category | 说明 |
|-----------|-------------|------|
| Concepts | concepts | 基础概念 |
| Introduction | concepts | 入门介绍 |
| DesignPatterns | architecture | 设计模式 |
| Architecture | architecture | 架构组件 |
| ReferenceArchitecture | architecture | 参考架构 |
| AgenticFrameworks | frameworks | Agent 框架 |
| WorkflowBuilders | frameworks | 工作流构建器 |
| AgentPlatforms | platforms | Agent 平台 |
| Standards | standards | 行业标准 |
| AgentHarness | harness | Agent Harness |
| AgenticTechStack | harness | 技术栈 |
| AgentMemory | memory | 记忆系统 |
| Planning & Reasoning | planning | （在精选层有，概念层分散在各目录） |
| Tool Use | tool-use | （在精选层有，概念层分散在各目录） |
| Multi-Agent Systems | multi-agent | （在精选层有，概念层在 Architecture/multi-agent 等） |
| EvaluationFrameworks | evaluation | 评估框架 |
| Benchmarks | benchmarks | 基准测试 |
| SafetyFrameworks | safety | 安全框架 |
| AIGovernance | safety | AI 治理（归入安全大类） |
| Observability | observability | 可观测性 |
| AgentOps | observability | Agent 运维（归入可观测） |
| ProductionBestPractices | production | 生产最佳实践 |
| MaturityModels | maturity-models | 成熟度模型 |
| ContextEngineering | context-engineering | 上下文工程 |
| PromptEngineering | prompt-engineering | 提示工程 |
| RAG | rag | 检索增强生成 |
| AICodingAgents | coding | 代码 Agent |
| Marketplace | platforms | 市场/平台（归入 platforms） |
| Wizard | applications-other | 向导式 Agent |
| AllThingsAnthropic | vendor-anthropic | Anthropic 专区 |
| AllThingsOpenAI | vendor-openai | OpenAI 专区 |
| AllThingsGoogle | vendor-google | Google 专区 |
| AllThingsMicrosoft | vendor-microsoft | Microsoft 专区 |
| AllThingsAWS | vendor-aws | AWS 专区 |
| Surveys & Position Papers | surveys | （精选层有，概念层分散在 Concepts/Introduction） |

### 概念层特殊处理

概念层有几个目录内容是跨分类的，入库时需要：
1. **按文档内容判断 category**：比如 AgenticFrameworks 下有 LangChain（frameworks）、CrewAI（multi-agent + frameworks）等，不能简单按目录名映射
2. **优先用 front matter 中的 tags**：每篇文档有 `tags: [concepts, agentic-ai]`，可以辅助分类
3. **实在无法自动分类的**：先归入 `architecture` 兜底，后续人工调整

---

## 四、精选论文层 → 统一分类映射

精选论文层有 10 个章节，映射到统一分类如下：

| 精选论文层章节 | 统一 category | 说明 |
|-------------|-------------|------|
| Surveys & Position Papers | surveys | 综述论文 |
| Agent Architectures & Frameworks | architecture | 架构与框架 |
| Planning & Reasoning | planning | 规划与推理 |
| Memory | memory | 记忆系统 |
| Tool Use | tool-use | 工具使用 |
| Multi-Agent Systems | multi-agent | 多 Agent |
| Interactive Environments | environments | 交互环境 |
| Applications | applications-other | 应用（先粗分，后续可细化） |
| Evaluation & Benchmarks | evaluation | 评估（benchmarks 也归入 evaluation） |
| Safety & Alignment | safety | 安全与对齐 |

### 精选论文层特殊处理

- **Applications** 章节内容杂（coding / research / product 都有），可以在入库时用 LLM 再细分到具体的应用分类
- **Architectures & Frameworks** 章节目录名是"架构与框架"，统一归入 `architecture`，框架相关的论文也在里面
- **Evaluation & Benchmarks** 合并成一个统一分类 `evaluation`，细分的话 benchmark 相关可以加 tag

---

## 五、追踪层 → 统一分类映射

追踪层来源是 arXiv，用 arXiv 原生分类粗筛：

| arXiv 分类 | 初筛后归入统一 category | 说明 |
|-----------|---------------------|------|
| cs.AI | 全部进入 LLM 过滤 | 人工智能，主分类 |
| cs.MA | multi-agent | 多智能体系统，直接入库 |
| cs.CL | context-engineering / rag | 计算语言学，相关性中等 |
| cs.LG | 过滤后按内容分 | 机器学习，相关性低，需严格过滤 |
| cs.IR | rag / memory | 信息检索，部分相关 |
| cs.HC | applications-other | 人机交互，部分相关 |
| cs.SE | coding | 软件工程 / 代码 Agent |

### 追踪层分类方式

追踪层数量多（几千篇）、质量参差，分类策略：
1. **粗筛**：按 arXiv 分类先过滤掉完全不相关的
2. **LLM 分类**：相关性过滤时顺便让 LLM 判断属于哪个统一分类
3. **分类置信度**：LLM 给出分类 + 置信度，高置信度的直接标，低置信度的标 `other`

---

## 六、与飞书知识地图的对应关系

飞书知识地图的结构和 RAG 库的统一分类完全对齐：

```
飞书一级标题           RAG 库顶层分类（category 前缀）
─────────────────────────────────────────────────
一、基础概念与分类  →  concepts / surveys
二、架构与核心模式  →  planning / memory / tool-use / multi-agent / evaluation / architecture
三、框架与平台      →  frameworks / platforms / harness
四、应用与场景      →  coding / research / product-pm / environments / applications-other
五、项目内部文档    →  （纯飞书内容，不在 RAG 库中）

跨领域分类（safety / observability / production / standards / ...）
  → 分布在各一级标题下，作为三级或四级知识点
```

**Agent 检索时的路径**：
1. 查飞书知识地图 → 拿到 category 列表（如 `["planning", "architecture"]`）
2. 用这些 category 在 RAG 库中做定向检索（metadata 过滤）
3. 检索结果按层加权排序
4. 返回结果，标注来源层和来源文档

---

## 七、实施建议

### 入库时的分类处理顺序

| 层 | 分类方式 | 工作量 |
|----|---------|--------|
| 概念层 | 目录名 + front matter tags 映射 → 人工复核 Top-20 文档 | 小 |
| 精选论文层 | 10 个章节直接映射到统一分类 | 极小 |
| 追踪层 | arXiv 粗分类 + LLM 判断细分类 | 中（自动化） |

### 先做什么

1. **精选论文层最容易**：10 个章节 → 10 个统一分类，映射关系清晰，先做
2. **概念层次之**：31 个目录 → 20+ 统一分类，有少数需要按内容判断，大部分目录名能直接映射
3. **追踪层最后**：需要 LLM 过滤 + 分类，等前两层跑通了再做

---

## 相关文档

- 《AI Agent 知识库 RAG 选型笔记》—— 选型二：知识源的选择
- 《飞书知识地图 — 目录结构设计》—— 飞书侧的 5 大类结构
- 《RAG 知识库实施指南 — 骨架实现篇》—— RAGStore 的 category 过滤

<!-- 三层知识库分类映射表 -->
