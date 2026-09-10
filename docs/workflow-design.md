# AI PM Agent — 节点级工作流设计

> 版本：v2.0（融入 G1-G9 + 对齐 technical-design.md）
> 日期：2026-09-08
> 状态：待用户确认
> 基于：10 个 PM skill 参考综合提炼 + AI PM 职能调研（G1-G9）
> 变更：v1.0→v2.0 新增 4 节点（kb_lookup / ai_feasibility_check / capability_boundary / kb_write_back），State 新增 G1-G9 字段，HITL 增强，节点总数 18→22 <!-- [MA 2026-09-08] -->

---

## 一、设计原则

1. **最简内核 + 状态机节点**：单 Agent，LangGraph 状态机驱动，节点即阶段
2. **不搞臃肿**：首版覆盖探索→PRD→评估→AI专项，22 个节点
3. **HITL 优先**：关键决策点（需求确认/做不做/章节裁剪/红队审查）必须人机交互
4. **产物自包含**：HTML 格式，可编辑/可打印/可复制，按需求名分文件夹
5. **质量门控**：每个产物节点输出前自检，红队审查独立于生产
6. **知识库全程横切（G4）**：开工前查家底，交付时写回，对业务节点透明
7. **AI 可行性先行（G5/G7）**：探索阶段判断"该不该用 AI 做"，机会评分算成本账
8. **能力边界即组件缺口（G8/G9）**：需求确认门输出三色表，组件候选先查开源 <!-- [MA 2026-09-08] -->

---

## 二、State Schema（LangGraph State）

> v2.0 更新：融入 G1-G9，新增 7 个字段 + 3 个字段增强。详见下方变更表。

```python
class PMState(TypedDict):
    # ─── 标识 ───
    initiative_id: str                    # 全流程唯一标识（兼作 SQLite thread_id）
    requirement_name: str                 # 需求名（文件夹名）
    current_stage: str                    # 当前阶段名（探索/PRD/评估/AI专项）

    # ─── 知识库底座（G4 横切：全程相伴）───
    kb_context: dict                      # 🆕 G4: 开工前查家底的检索结果（选型/评测/案例）
    kb_written_back: list                 # 🆕 G4: 本流程已写回知识库的档案 ID 列表

    # ─── 探索阶段 ───
    raw_requirement: str                  # 用户原始需求描述
    info_completeness: dict               # 6 维度信息完整度评估
    confirmed_requirement: str            # 用户确认后的需求
    user_insights: dict                   # 从用户脑中挖出的信息
    external_evidence: dict               # 外部验证证据（Reddit/竞品/数据）
    opportunity_score: dict               # 机会评分（ODI/RICE）+ 🆕 G7: cost_feasibility
    competitor_teardown: dict             # 竞品拆解结果
    ai_feasibility: dict                  # 🆕 G5: 必须AI做 / 传统就能做 / AI反而更差
    capability_boundary: dict             # 🆕 G8: 自动/工具/人工 三色表
    component_candidates: list            # 🆕 G8+G9: 可插拔组件候选 + 开源优先检查结果
    proceed_decision: bool | None         # 是否继续做（用户决策）

    # ─── 评测集（G2 全程生长）───
    eval_cases: list                      # 🆕 G2: 需求确认门后建立，每个节点可追加用例

    # ─── PRD 阶段 ───
    section_plan: dict                    # 章节裁剪计划（🔒 G6: AI功能异常边界默认必出）
    section_confirmed: bool               # 章节裁剪是否经用户确认
    prd_html: str                         # PRD HTML 内容
    prd_sections: list                     # PRD 章节列表
    red_team_review: dict                 # 红队审查反馈（含 has_changes 标志）
    prd_revision_count: int               # PRD 修订次数（上限 3 轮）

    # ─── 评估阶段 ───
    metrics_tree: dict                    # 指标树（North Star + drivers + guardrails）
    experiment_design: dict              # 实验设计（可证伪假设+统计功效+实验分组）
    ai_eval_design: dict                 # AI 评估设计（eval cases + rubric + 阈值，与 eval_cases 联动）

    # ─── AI 专项 ───
    model_selection: dict                 # 🔒 G1: 循环化（查档案→bake-off→写回→出结论）
    failure_modes: dict                  # 失败模式分析 + 缓解方案

    # ─── 产物管理 ───
    artifacts: dict                       # 产物文件路径映射
    human_feedback: list                  # 所有人机交互记录

    # ─── 交付后接口（G3 二期留接口）───
    post_launch_hooks: dict | None       # 🆕 G3: bad case 回收 / 回归重跑 接口占位
```

**v1.0→v2.0 State 变更表：**

| 新增字段 | 缺口 | 说明 |
|---|---|---|
| `kb_context` | G4 | 开工前检索知识库，全程可读 |
| `kb_written_back` | G4 | 交付时写回，闭环 |
| `ai_feasibility` | G5 | 探索阶段判断"该不该用 AI 做" |
| `capability_boundary` | G8 | 需求确认门输出三色表 |
| `component_candidates` | G8+G9 | 组件候选清单 + 开源优先检查 |
| `eval_cases` | G2 | 需求确认后建立，全流程生长 |
| `post_launch_hooks` | G3 | 二期接口占位 |

| 增强字段 | 缺口 | 变更 |
|---|---|---|
| `opportunity_score` | G7 | 增加 `cost_feasibility` 维度 |
| `section_plan` | G6 | AI 功能"异常与边界"章默认必出（兜底/降级/拒绝规则） |
| `model_selection` | G1 | 从单节点改为循环结构（查档案→bake-off→写回→出结论） | <!-- [MA 2026-09-08] -->

---

## 三、节点定义

### 3.1 探索阶段（A）

| 节点 ID | 节点名 | 类型 | 职责 | 输入 | 输出 |
|---|---|---|---|---|---|
| `kb_lookup` | 🆕 查家底 | 自动 | G4: 检索知识库中相关选型/评测/案例背景 | raw_requirement | kb_context |
| `intake` | 需求接收 | 自动 | 接收用户需求，6维度评估信息完整度 | raw_requirement, kb_context | info_completeness |
| `requirement_confirm` | 需求确认门 | 🔴 HITL | 信息不足追问3-5问；信息充足时结构化复述+章节裁剪预告+**G8能力边界三色表**，请用户确认；确认后**G2建初始考题集** | info_completeness | confirmed_requirement, proceed_decision, capability_boundary, eval_cases |
| `needs_discovery` | 需求挖掘 | 自动 | 从用户脑中挖信息：谁/什么场景/痛点/现有方案/差距 | confirmed_requirement | user_insights |
| `external_validation` | 外部验证 | 自动 | Reddit痛点+竞品差距+ODI机会评分+**G7成本可行性** | user_insights | external_evidence, opportunity_score（含 cost_feasibility） |
| `ai_feasibility_check` | 🆕 AI可行性判断 | 自动 | G5: 判断"必须AI做/传统就能做/AI做了反而更差" | external_evidence, opportunity_score | ai_feasibility |
| `competitor_teardown` | 竞品拆解 | 自动 | 15章系统分析（Snapshot→JTBD→Core Loop→...→Verdict） | confirmed_requirement | competitor_teardown |
| `capability_boundary` | 🆕 能力边界+组件候选 | 自动 | G8: 逐步骤判自动/工具/人工；G9: 组件候选先查开源与MCP生态 | ai_feasibility, competitor_teardown | capability_boundary, component_candidates |
| `strategy_decision` | 策略决策门 | 🔴 HITL | 综合探索结果（含ai_feasibility+capability_boundary），用户决定"做不做" | user_insights, external_evidence, opportunity_score, competitor_teardown, ai_feasibility, capability_boundary | proceed_decision |

### 3.2 PRD 阶段（C）

| 节点 ID | 节点名 | 类型 | 职责 | 输入 | 输出 |
|---|---|---|---|---|---|
| `section_planning` | 章节裁剪规划 | 自动 | 根据需求大小判断保留/省略哪些章节（4恒定+8可选）；**🔒 G6: AI功能"异常与边界"章默认必出**（兜底/降级/拒绝规则） | confirmed_requirement | section_plan |
| `section_confirm` | 章节确认门 | 🔴 HITL | 列出保留/省略章节及原因，请用户确认 | section_plan | section_confirmed |
| `prd_generation` | PRD 生成 | 自动 | 生成 HTML PRD：结构化分块（页面元素+交互说明必出），禁止技术接口表述 | confirmed_requirement, section_plan, user_insights | prd_html, prd_sections |
| `red_team_review` | 红队审查 | 🔴 HITL | 三视角审查：用户视角/失败倒推(pre-mortem)/VP视角 | prd_html | red_team_review |
| `prd_revision` | PRD 修订 | 自动 | 根据红队反馈修订 PRD | prd_html, red_team_review | prd_html(修订版), prd_revision_count+1 |

### 3.3 评估阶段（E）

| 节点 ID | 节点名 | 类型 | 职责 | 输入 | 输出 |
|---|---|---|---|---|---|
| `metrics_tree` | 指标树设计 | 自动 | North Star + 3 input metrics + 1 guardrail + 1 blindspot | confirmed_requirement, prd_html | metrics_tree |
| `experiment_design` | 实验设计 | 自动 | 可证伪假设+统计功效+实验分组 | metrics_tree | experiment_design |
| `ai_eval_design` | AI 评估设计 | 自动 | eval cases + 评分 rubric + 验收阈值 | prd_html, model_selection | ai_eval_design |

### 3.4 AI 专项（H）

| 节点 ID | 节点名 | 类型 | 职责 | 输入 | 输出 |
|---|---|---|---|---|---|
| `model_selection` | 模型选型权衡 | 自动 | **🔒 G1: 循环化** — 查评测档案→无/过期则 bake-off（用 eval_cases 跑对比）→写回档案→出结论；latency/cost/accuracy 三维权衡 | confirmed_requirement, eval_cases | model_selection |
| `failure_analysis` | 失败模式分析 | 自动 | 幻觉/上下文溢出/分布偏移/偏见等，每类给缓解方案 | prd_html, model_selection | failure_modes |

### 3.5 产物管理

| 节点 ID | 节点名 | 类型 | 职责 | 输入 | 输出 |
|---|---|---|---|---|---|
| `artifact_persist` | 产物持久化 | 自动 | 按需求名分文件夹保存所有 HTML 产物 | 所有产物 | artifacts |
| `kb_write_back` | 🆕 知识库写回 | 自动 | G4: 本流程的决策/评测/案例结论写回知识库，闭环 | artifacts, model_selection, ai_feasibility | kb_written_back |

---

## 四、边与流转（Edges）

```
                    ┌──────────────────────────────────────────────────┐
                    │              探索阶段 (A)                        │
                    │                                                  │
        kb_lookup ──► intake ──► requirement_confirm ──► needs_discovery │
         (G4查家底)               (HITL, G8/G2)              │           │
                                                            ▼           │
                                                    external_validation │
                                                    (含G7成本)    │     │
                                                                  ▼     │
                                                  ai_feasibility_check   │
                                                       (G5)      │       │
                                                                  ▼       │
                                                    competitor_teardown  │
                                                              │           │
                                                              ▼           │
                                                    capability_boundary   │
                                                       (G8/G9)    │       │
                                                                  ▼       │
                                                    strategy_decision     │
                                                    ──── No ──► END    │
                                                         │              │
                    └─────────────────────────────────────┼──────────────┘
                                                              │ Yes
                                                              ▼
                    ┌──────────────────────────────────────────────────┐
                    │              PRD 阶段 (C)                       │
                    │     (G6: AI功能异常边界默认必出)                 │
                    │                                                  │
              section_planning ──► section_confirm                    │
                                    │                                │
                                    ▼                                │
                              prd_generation                         │
                                    │                                │
                                    ▼                                │
                              red_team_review                         │
                                 │        │                          │
                                 │   No changes                      │
                                 ▼        │                          │
                           prd_revision    │                          │
                                 │        │                          │
                    └───────────────┼────┘──────────────────────────┘
                                    │
                                    ▼
                    ┌──────────────────────────────────────────────────┐
                    │            AI 专项 + 评估 (H+E)                 │
                    │                                                  │
              model_selection ──► failure_analysis                   │
              (G1循环化)               │                             │
                                       ▼                             │
                                 metrics_tree                        │
                                       │                             │
                                       ▼                             │
                                experiment_design                    │
                                       │                             │
                                       ▼                             │
                                 ai_eval_design                     │
                                       │                             │
                    └────────────────────┼──────────────────────────┘
                                         │
                                         ▼
                              artifact_persist
                                         │
                                         ▼
                                kb_write_back ──► END
                                  (G4写回)
```

**关键条件边：**
- `requirement_confirm` → 若用户拒绝 → END（需求不成立）
- `strategy_decision` → `proceed_decision=False` → END（不做）
- `strategy_decision` → `proceed_decision=True` → `section_planning`
- `red_team_review` → 无修改意见 → `model_selection`
- `red_team_review` → 有修改意见 → `prd_revision` → 回到 `red_team_review`（循环，上限 3 轮）
- `prd_revision` 超过 3 轮 → END（上报用户决策）
- `section_confirm` → 用户要求重裁 → 回到 `section_planning` <!-- [MA 2026-09-08] -->

---

## 五、HITL 中断点详解

### 5.1 requirement_confirm（需求确认门）

**触发**：intake 节点完成信息完整度评估后

**逻辑**：
- 6 维度评估：问题背景/目标/用户场景/现有方案/业务规则/参考方向
- 信息不足 → 追问 3-5 个具体问题（不重复已有信息）
- 信息充足 → 结构化复述（要做什么/不做什么/关键规则/成功指标）+ 章节裁剪预告
- **🆕 G8 能力边界三色表**：逐步骤判 自动/工具/人工，展示给用户确认
- 用户确认 → proceed；用户拒绝或补充 → 重新评估
- **🆕 G2 考题前置**：用户确认后立即建初始评测集（`init_eval_cases()`），后续节点可追加

**LangGraph 实现**：`interrupt()` 等待用户输入；恢复后调 `init_eval_cases(state)` 初始化 `eval_cases` <!-- [MA 2026-09-08] -->

### 5.2 strategy_decision（策略决策门）

**触发**：探索阶段全部完成后

**逻辑**：
- 汇总 user_insights + external_evidence + opportunity_score + competitor_teardown
- **🆕 G5/G7**：汇总中包含 ai_feasibility（该不该用AI做）+ opportunity_score.cost_feasibility（成本可行性）
- **🆕 G8**：汇总中包含 capability_boundary（三色表）+ component_candidates（组件候选+开源检查）
- 给出"建议做/建议不做/需要更多信息"的结论及理由
- 用户决策：做/不做/补充探索

**LangGraph 实现**：`interrupt()` 等待用户输入

### 5.3 section_confirm（章节确认门）

**触发**：section_planning 完成后

**逻辑**：
- 列出本次 PRD 保留哪些章节、省略哪些、各自原因
- 核心 4 章恒定：项目信息+版本记录/需求背景/需求目标/详细方案
- 8 章可选：需求概述/流程图/交互流程图/异常边界/数据埋点/时序图/上线计划/附录
- 用户可要求保留某章

**LangGraph 实现**：`interrupt()` 等待用户确认

### 5.4 red_team_review（红队审查）

**触发**：prd_generation 完成后

**逻辑**：
- 三视角独立审查：
  1. **用户视角**（customer-voice）：作为目标用户读 PRD，指出哪里会困惑/哪里不想用
  2. **失败倒推**（pre-mortem）：假设上线已失败，倒推 5 条失败路径（需求/执行/分发/组织/外部）
  3. **VP 视角**（exec-reviewer）：90 秒 skim，列出 5 个会被追问的问题
- 输出：问题清单 + 严重程度 + 修改建议
- 用户决策：采纳哪些修改 / 直接通过 / 放弃

**LangGraph 实现**：`interrupt()` 等待用户决策；修订循环上限 3 轮，超限 → END 上报用户

---

## 六、产物结构（文件组织） <!-- [MA 2026-09-08] -->

### 6.1 按需求名分文件夹（谁先开工谁建家）

```
项目根/
├── [需求名]/                      # 一个需求一个顶层目录
│   ├── 需求文档/
│   │   └── [需求名]-PRD.html      # 自包含 HTML PRD
│   ├── 竞品拆解/
│   │   └── [需求名]-teardown.html # 竞品拆解报告
│   ├── 需求洞察/
│   │   └── [需求名]-insights.html # 探索阶段产物
│   ├── 评估方案/
│   │   ├── [需求名]-metrics.html  # 指标树
│   │   ├── [需求名]-experiment.html # 实验设计
│   │   └── [需求名]-ai-eval.html  # AI 评估设计
│   ├── AI分析/
│   │   ├── [需求名]-model-selection.html # 模型选型
│   │   └── [需求名]-failure-modes.html   # 失败模式
│   └── 沟通记录.md                # 人机交互记录
├── scripts/                       # 共享：HTML 服务/导出
└── .handoff/                      # 共享：跨会话交接
```

### 6.2 HTML 产物规范

- **自包含**：内嵌 CSS，无外部依赖，脱机可用
- **全文档可编辑**：`contenteditable`，保存抓 `outerHTML` 覆盖原文件
- **一键复制**：iframe→base64 图片，三档降级
- **禁止技术接口表述**：描述列不出现 API/参数/字段/错误码

---

## 七、PRD 章节结构（按需裁剪）

### 7.1 核心骨架（任何需求都必出）

| 章节 | 说明 |
|---|---|
| 📋 项目信息 + 版本记录 | 文档元信息 + 变更历史 |
| 一、需求背景 | 为什么要做 |
| 二、需求目标 | 要达成什么 |
| 三、详细方案 | 四列表格：一级模块/二级功能/原型/描述 |

### 7.2 按需裁剪（8 章可选）

| 章节 | 省略条件 |
|---|---|
| 需求概述 | 功能点极少时 |
| 流程图 | 无多步骤流程 |
| 交互流程图 | 核心界面 ≤2 个 |
| 异常与边界 | 无新异常场景；**🔒 G6: AI 功能此章默认必出**（兜底/降级/拒绝回答规则） |
| 数据埋点 | 无新增埋点 |
| 时序图 | 无复杂端侧编排 |
| 上线计划 | 直接全量上线 |
| 附录 | 无决策项 |

### 7.3 详细方案描述列结构化分块

**必出块**：
- 【页面元素】：所有元素自上而下说明
- 【交互说明】：操作→结果 句式

**按需块**：
- 【功能逻辑】、【边界说明】、【数据与内容规则】、【前置条件与权限】、【文案规范】

---

## 八、质量门控

### 8.1 节点自检（每个产物节点输出前）

| 节点 | 自检项 |
|---|---|
| prd_generation | 描述列无技术接口表述；必出块齐全；HTML 自包含；**🔒 G6: AI功能有异常边界章节** |
| metrics_tree | North Star 是价值单位非虚荣指标；有 guardrail |
| experiment_design | 假设可证伪；有统计功效说明 |
| ai_eval_design | eval cases 覆盖 happy/edge/adversarial；有评分 rubric；**与 eval_cases 联动**（G2） |
| model_selection | **🔒 G1: 有循环记录**（查档案→bake-off→写回）；有 latency/cost/accuracy 三维对比 |
| failure_analysis | 每类失败模式有缓解方案 |
| 🆕 ai_feasibility_check | **G5**: 给出明确分类（必须AI/传统即可/AI更差）+ 理由 |
| 🆕 capability_boundary | **G8**: 三色表覆盖所有步骤；**G9**: 组件候选有开源检查记录 |
| 🆕 external_validation | **G7**: opportunity_score 含 cost_feasibility |
| 🆕 kb_lookup | **G4**: 即使无匹配也返回空结果（不阻塞流程） |
| 🆕 kb_write_back | **G4**: 至少写回 1 份档案（决策/评测/案例） |

### 8.2 红队审查（独立于生产）

三视角独立审查，不替作者改，只指出问题。

---

## 九、G1-G9 缺口落地映射

> 本表是 v2.0 相对 v1.0 的核心增量，每个缺口的具体技术实现见 [technical-design.md](technical-design.md) 第五节。

| 缺口 | 落地节点/机制 | State 字段 | 变更类型 |
|---|---|---|---|
| G1 选型循环化 | `model_selection` 节点内子图（查档案→bake-off→写回→出结论） | `model_selection` 改循环结构 | 改造 |
| G2 考题前置 | `requirement_confirm` HITL 恢复后 `init_eval_cases()` | `eval_cases` 新增 | 新增 |
| G3 上线后回流 | `post_launch_hooks` 字段占位 + `artifact_persist` 预留接口 | `post_launch_hooks` 新增 | 二期留接口 |
| G4 知识库底座 | `kb_lookup` 节点（开工前查）+ `kb_write_back` 节点（交付时写回）+ NodeRunner 内自动检索 | `kb_context` + `kb_written_back` 新增 | 新增 |
| G5 AI 可行性 | `ai_feasibility_check` 节点（探索阶段独立判断） | `ai_feasibility` 新增 | 新增 |
| G6 失败兜底进 PRD | `section_planning` 规则：AI 功能"异常与边界"章默认必出 | `section_plan` 增强 | 规则 |
| G7 成本入评分 | `external_validation` 节点输出 `cost_feasibility` | `opportunity_score` 增强 | 增强 |
| G8 能力边界评估 | `capability_boundary` 节点 + `requirement_confirm` HITL 展示 | `capability_boundary` + `component_candidates` 新增 | 新增 |
| G9 造轮子检查 | `capability_boundary` 节点内：组件候选先查 GitHub/MCP 生态 | `component_candidates` 含开源检查 | 新增 |

<!-- [MA 2026-09-08] v2.0 融入 G1-G9，节点 18→22，State 新增 7 字段+3 增强 -->

---

## 十、参考来源映射

| 设计决策 | 来源 skill |
|---|---|
| 按需求名分文件夹 + 谁先开工谁建家 | PM-K12--skills |
| 章节按需裁剪 + 显式确认 | PM-K12--skills |
| 描述列结构化分块 + 禁止技术接口 | PM-K12--skills |
| 全文档可编辑 + outerHTML 持久化 | PM-K12--skills |
| iframe→base64 一键复制 + 三档降级 | PM-K12--skills |
| 需求挖掘两拍（脑中挖+外部验证） | vibe-check |
| 竞品拆解 15 章框架 | product-teardown-skill |
| 多 Agent 管道→单 Agent 状态机映射 | pm-agent-harness-kit |
| 红队三视角审查 | 10x-pm |
| 指标树 + 实验设计 | 10x-pm |
| grill-me 压力测试理念 | ai-product-manager-skills |
| AI 评估设计 | ai-product-manager-skills, Product-Manager-Skills |
| 模型选型 + 失败模式 | ai-product-manager-skills |
| 视角转换（工程师版/老板版） | product-management-skill |

---

<!-- [MA 2026-09-08] -->
