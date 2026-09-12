# RAG 知识库实施指南 — 自动更新篇

> 本文档细化三层知识库的自动更新机制，重点是追踪层的 arXiv 每周增量更新。
> 对应选型文档：《AI Agent 知识库 RAG 选型笔记》选型七

---

## 一、三层更新策略总览

| 知识层 | 更新频率 | 更新方式 | 人工介入 | 优先级 |
|--------|---------|---------|---------|--------|
| 概念解读层 | 每季度一次 | 手动检查新章节，选择性吸收 | 全程手动 | 低 |
| 精选论文层 | 每月一次 | 对比 awesome 列表 diff → 补 abstract → 入库 | 人工审核新增论文 | 中 |
| 新论文追踪层 | 每周一次 | arXiv API 拉取 → LLM 过滤 → 自动入库 | LLM 自动过滤，偶尔抽查 | 高 |

---

## 二、持续更新下的 Chunk 策略

### 2.1 为什么这是个问题

如果知识库是固定不变的（一次性导入后不动了），分块策略很简单——一次分好就完了。
但我们的知识库是**持续更新**的，三层都有各自的更新节奏，这就带来几个问题：

1. **新增内容怎么分块**——和已有内容用同样的分块规则吗？
2. **更新内容怎么替换旧 chunk**——是整篇删了重写，还是只变更动的部分？
3. **分块策略本身升级了怎么办**——比如发现 section-aware 效果不好，要改分块大小，已有的 chunk 要不要全部重建？
4. **跨层内容迁移**——追踪层的论文被精选层收录了，怎么从 tracked 升到 curated？

### 2.2 各层的 Chunk 与更新方式

| 知识层 | 分块策略 | 更新时的 Chunk 处理 | 更新频率 |
|--------|---------|-------------------|---------|
| 概念解读层 | Section-aware（按标题切，500-800字） | **整篇文档级替换**：文档更新时，先删除该 source 的所有旧 chunk，再重新分块入库 | 每季度 |
| 精选论文层 | 整篇论文 = 一个 chunk | **单篇级 upsert**：新增论文直接入库；论文有更新（新版本）时按 arxiv_id 覆盖更新 | 每月 |
| 新论文追踪层 | 整篇论文 = 一个 chunk | **批量追加**：每周新增一批，直接追加入库；不修改已有的 | 每周 |

### 2.3 为什么概念层用整篇替换，而不是增量更新

概念层是 section-aware 分块，一篇文档会被切成多个 chunk。如果文档更新了一个小节，理论上可以只更新那个小节对应的 chunk。但我们选择**整篇删了重写**，原因：

1. **更新频率极低**（每季度一次）——为这个做增量机制不值得
2. **文档量小**（200 篇左右）——全量重建也很快，几分钟的事
3. **简单可靠**——整篇替换不会出现"旧 chunk 残留"或"分块边界错乱"的问题
4. **结构可能大变**——概念层的更新经常是新增/删除章节、调整结构，不是改几句话，增量处理反而复杂

### 2.4 论文层为什么整篇一个 chunk

论文层（精选层 + 追踪层）每篇论文 = 一个 chunk，不分细块。更新时也是整篇替换：

1. **abstract 本身就短**（150-300 词）——不需要切
2. **语义完整**——一篇论文的 abstract 是一个完整的知识单元，切开反而破坏语义
3. **更新简单**——按 arxiv_id upsert 就行，不需要管理 chunk 索引
4. **未来要加全文的话再说**——目前只入库 abstract，等以后有全文 PDF 了再考虑细粒度分块

### 2.5 跨层迁移：从 tracked 到 curated

当一篇追踪层的论文被 awesome 列表收录（进入精选层）时：

1. **操作**：先删除 tracked-paper 层中该 arxiv_id 的 chunk，再在 curated-paper 层插入
2. **元数据升级**：layer 从 `tracked-paper` 改为 `curated-paper`，curated 标记从 false 改为 true，category 用精选层的分类（可能更准确）
3. **embedding 可以复用**——内容没变，向量也没变，理论上可以直接改 metadata。但 ChromaDB 的 upsert 是按 ID 的，直接改 ID 对应的 metadata 更高效
4. **频率**：每月一次（和精选层更新同步），每次可能几篇到十几篇，量很小

### 2.6 分块策略升级时的全量重建

如果以后发现分块策略需要调整（比如 chunk 大小改大改小、分块算法换了）：

1. **触发条件**：分块策略版本号变更（如 v1 → v2）
2. **操作方式**：全量重建——清空 collection → 所有源文档重新分块 → 重新嵌入 → 重新入库
3. **成本估算**：
   - 概念层：209 篇文档 × 平均 5 个 chunk = ~1000 个嵌入
   - 精选层：517 篇论文 = 517 个嵌入
   - 追踪层：3000-5000 篇 = 3000-5000 个嵌入
   - 总计约 4000-6500 个嵌入，按 Doubao embedding 价格计算，约几毛钱到几块钱
4. **时间**：嵌入 API 有速率限制，批量处理约 30-60 分钟
5. **建议**：在配置里加一个 `chunking_version` 字段，入库时写进每个 chunk 的 metadata，需要全量重建时对比版本号

### 2.7 Chunk ID 设计

稳定的 chunk ID 对更新和去重很重要。ID 设计规则：

```
概念层：  concept-{source_file_path_hash}-{chunk_index}
精选层：  curated-{arxiv_id}
追踪层：  tracked-{arxiv_id}
```

- 概念层用文件路径 hash + chunk 索引，保证同一文件同一分块策略下 ID 稳定
- 论文层直接用 arxiv_id，简单好记，跨层迁移时 ID 前缀变一下就行
- 版本升级时（chunking_version 变了），ID 里加版本后缀，避免新旧混杂

---

## 三、追踪层：arXiv 每周增量更新

### 2.1 整体流程

```
每周一 08:00 触发（可手动触发）
  │
  ├── 1. 确定时间范围
  │     上次更新时间 ~ 现在（默认 7 天）
  │
  ├── 2. arXiv API 拉取
  │     按关键词 + 分类筛选拉取论文列表
  │     约 200-500 篇/周（全 cs 领域）
  │
  ├── 3. 去重
  │     用 arXiv ID 与已有追踪层比对，去掉已存在的
  │
  ├── 4. LLM 相关性过滤
  │     每篇论文：title + abstract → LLM 判断是否 agent 相关
  │     输出：relevant / not_relevant + 置信度 + 分类建议
  │
  ├── 5. 批量入库
  │     过滤后约 30-80 篇/周 → 嵌入 → 写入 ChromaDB 追踪层
  │
  └── 6. 生成更新报告
        新增论文数 / 过滤掉的数 / 高置信度论文列表
        保存为 weekly_update_YYYYMMDD.md
```

### 2.2 arXiv API 使用说明

**API 端点**：
```
http://export.arxiv.org/api/query
```

**查询参数**：
- `search_query`：搜索关键词，支持分类限定
- `start` / `max_results`：分页
- `sortBy`：`submittedDate`（按提交时间排序）
- `sortOrder`：`descending`

**分类筛选**：
arXiv 的计算机科学分类中，与 agent 相关的主要有：
- `cs.AI` — 人工智能（最相关，量大）
- `cs.CL` — 计算语言学（NLP 相关，部分相关）
- `cs.LG` — 机器学习（方法层面相关，量大但杂）
- `cs.MA` — 多智能体系统（直接相关，量小）

**建议查询策略**：
1. 先限定 `cs.AI` + `cs.MA` + `cs.CL`，按关键词"agent"、"LLM"、"tool use"、"planning"等组合查询
2. 拉取过去 7 天的论文，按提交时间倒序
3. 每次最多拉 200 篇，分页获取

**示例查询**：
```
search_query=cat:cs.AI AND (abs:agent OR abs:LLM OR abs:"tool use")
&sortBy=submittedDate&sortOrder=descending&max_results=200
```

**注意事项**：
- arXiv API 有速率限制：每 3 秒不超过 1 次请求
- 返回格式是 Atom XML，需要解析
- 论文可能有多个版本，取最新版本的提交时间

### 2.3 LLM 相关性过滤

**为什么需要过滤**：
- 直接按关键词拉的论文噪声很大，很多只是提了一句"agent"但主题不相关
- 全量入库会稀释检索质量，追踪层本身权重就低，再灌水就没用了
- 每周 200-500 篇，人工审核成本太高

**过滤方案**：

```python
# 伪代码
def filter_paper(paper, llm):
    prompt = f"""
你是 AI Agent 领域论文筛选器。请判断以下论文是否与"基于大语言模型的 AI Agent"
主题高度相关。

论文标题：{paper['title']}
论文摘要：{paper['abstract']}

请判断：
1. 这篇论文是否以 LLM-based Agent 为核心研究主题？
2. 如果相关，属于哪个分类（planning / memory / tool-use / multi-agent / evaluation / other）？
3. 你的置信度（high / medium / low）？

只返回JSON：
{{
  "relevant": true/false,
  "category": "planning|memory|tool-use|multi-agent|evaluation|other",
  "confidence": "high|medium|low",
  "reason": "一句话理由"
}}
"""
    return llm(prompt)
```

**过滤阈值**：
- `relevant = true` 且 `confidence = high` → 直接入库
- `relevant = true` 且 `confidence = medium` → 入库但标记 `pending_review`
- `relevant = true` 且 `confidence = low` → 丢弃
- `relevant = false` → 丢弃

**成本估算**：
- 每周 200-500 篇论文需要过滤
- 每篇约 500-1000 token 输入 + 100 token 输出
- 用 deepseek-v4-flash：约 0.05-0.15 元/周，成本极低
- **可以用更小的模型做初筛**，比如 deepseek-v4-flash 已经足够便宜了

**批量优化**：
- 可以一次塞 3-5 篇论文让 LLM 批量判断，减少 API 调用次数
- 但注意不要超过模型的上下文窗口，且批量判断准确率可能略降

### 2.4 去重机制

**去重维度**：
1. **arXiv ID**：同一篇论文的不同版本，只保留最新版本
2. **标题相似度**：有些论文可能不在 arXiv 上（但我们只追踪 arXiv，所以这条暂时不需要）

**实现方式**：
- 入库前查询 ChromaDB，检查 `arxiv_id` metadata 字段是否已存在
- 已存在则跳过（或比较版本号，更新版本）

### 2.5 入库流程

```
待入库论文列表
  │
  ├── 1. 补全元数据
  │     确保有：arxiv_id / title / authors / abstract /
  │             categories / published / updated / url
  │
  ├── 2. 分类标注
  │     用 LLM 过滤时返回的 category 字段
  │     没有的话标 "other"
  │
  ├── 3. 构造 chunk
  │     整篇论文 = 一个 chunk（论文层不做细粒度分块）
  │     metadata 含全部字段 + layer = "tracked-paper" + curated = false
  │
  ├── 4. 批量嵌入
  │     调用 EmbeddingModel.embed()
  │     分批处理，每批 25 篇
  │
  └── 5. 写入 ChromaDB
        用 upsert（存在则更新，不存在则插入）
```

### 2.6 更新报告

每次更新后生成一份 Markdown 报告，保存在 `domain_kb/update_logs/` 下：

```
# 周更新报告 — 2026-09-08 ~ 2026-09-15

## 概览
- 拉取论文数：327 篇
- 去重后新增：289 篇
- LLM 过滤后入库：47 篇（16.3%）
  - high 置信度：31 篇
  - medium 置信度：16 篇

## 高置信度新增论文（精选）

### Planning
- [Paper Title](https://arxiv.org/abs/xxxx.xxxxx) — 一句话亮点
- ...

### Tool Use
- ...

## 待人工复核（medium 置信度）
- ...

## 过滤掉的典型论文（抽样 5 篇）
- （供人工校准过滤阈值用）
- ...
```

---

## 四、精选层：每月 diff 更新

### 3.1 流程

```
每月 1 日触发
  │
  ├── 1. 获取最新版 awesome-llm-agent-papers 的 README.md
  │     （从 GitHub 拉 raw 文件）
  │
  ├── 2. 解析出论文列表（arxiv_id, title, category）
  │
  ├── 3. 与当前精选层对比，找出新增论文
  │
  ├── 4. 对新增论文：用 arXiv API 补全 abstract + 元数据
  │
  ├── 5. 人工审核（可选）
  │     因为每月新增不多（5-15 篇），可以人工过一遍
  │
  └── 6. 入库到 curated-paper 层
```

### 3.2 注意事项

- awesome 列表的分类质量很高，可以直接复用它的 taxonomy 作为 category
- 新增论文可能已经在追踪层里了（追踪层更快），入库时注意去重并提升层级（从 tracked 升到 curated）
- 层级提升的操作：删除 tracked-paper 中的旧 chunk，在 curated-paper 层插入新 chunk

---

## 五、概念层：季度手动更新

### 4.1 流程

```
每季度第一个周末
  │
  ├── 1. 检查 agentic-ai-knowledge-base 的 release / changelog
  │
  ├── 2. 浏览新增章节和重大更新
  │
  ├── 3. 选择性拉取新文档到本地 source/concept/
  │
  ├── 4. 人工阅读 + 做笔记 + 更新飞书知识地图
  │
  └── 5. 重新入库概念层（--clean 模式全量重建）
```

### 4.2 为什么手动

- 概念层质量要求最高，权重也最高（×2.0）
- 概念体系变化慢，不需要频繁更新
- 外部文档的结构和质量参差不齐，需要人工判断是否值得纳入

---

## 六、脚本设计

### 5.1 新增脚本

```
scripts/
├── rag_weekly_update.py    每周增量更新（追踪层）
├── rag_monthly_update.py   每月 diff 更新（精选层）
└── rag_quarterly_update.py 季度手动更新辅助工具（概念层）
```

### 5.2 rag_weekly_update.py

**用法**：
```bash
# 自动运行完整流程（拉取 → 过滤 → 入库 → 生成报告）
python scripts/rag_weekly_update.py

# 只拉取 + 过滤，不入库（预览模式）
python scripts/rag_weekly_update.py --dry-run

# 指定时间范围
python scripts/rag_weekly_update.py --from 2026-09-01 --to 2026-09-08

# 使用不同的 LLM provider 做过滤
python scripts/rag_weekly_update.py --filter-provider deepseek
```

**输出**：
- 更新报告 Markdown 文件
- 控制台打印摘要

### 5.3 调度方式

**方案 A：手动触发**（初期推荐）
- 每周一早上手动跑一次脚本
- 优点：简单、可控、出了问题好调试
- 缺点：容易忘

**方案 B：定时任务**（稳定后）
- Windows Task Scheduler 或 cron
- 每周一 08:00 自动执行
- 执行结果发通知（飞书消息 / 邮件）
- 优点：自动化
- 缺点：需要配置、出了问题可能不知道

**建议**：先手动跑 4 周，稳定了再加定时任务。

---

## 七、质量保障

### 6.1 过滤准确率监控

- 每月抽查 20 篇被过滤掉的论文，人工判断是否误判
- 统计 precision（过滤掉的里面确实不相关的比例）
- 统计 recall（相关的论文有没有被漏掉，这个比较难评估，可以用精选层做基准）

### 6.2 检索效果评估

- 准备 10-20 个标准查询 + 预期结果列表
- 每次更新后跑一遍查询，看检索质量有没有下降
- 如果下降明显，检查是不是新增了太多低质量论文

### 6.3 回滚机制

- 每次更新前备份 ChromaDB（直接复制 chroma/ 目录）
- 如果更新后发现问题，可以快速回滚到上一个版本
- 保留最近 4 周的备份

---

## 八、实施优先级

| 阶段 | 内容 | 时机 | 依赖 |
|------|------|------|------|
| P0 | 手动入库概念层 + 精选层 | 骨架实现阶段 | RAG 骨架 |
| P1 | 每周更新脚本（arXiv 拉取 + LLM 过滤 + 入库） | 骨架跑通后第 2 周 | P0 + LLM API |
| P2 | 更新报告 + 质量监控 | P1 稳定后 | P1 |
| P3 | 每月 diff 更新（精选层） | 有精选层种子数据后 | P0 + GitHub API |
| P4 | 自动定时任务 | 手动跑 4 周稳定后 | P1 |

---

## 相关文档

- 《AI Agent 知识库 RAG 选型笔记》—— 选型七：自动更新机制
- 《RAG 知识库实施指南 — 骨架实现篇》—— 核心模块设计
- 《飞书知识地图 — 目录结构设计》—— 飞书侧结构

<!-- [W02 2026-09-12] RAG 知识库自动更新机制方案 -->
