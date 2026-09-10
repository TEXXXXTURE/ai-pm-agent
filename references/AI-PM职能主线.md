# AI PM 主要工作职能主线（对照基准）

> 用途：判断哪些 PM 职能该建成**确定性流水线**（固定流程化、推进项目主体进度），哪些该交给 **Pi 自主模式 + 菜谱技能**灵活处理。建流水线前先查本表。
> 来源概括：`Product-Manager-Skills` 的 Component/Interactive/Workflow 三分法 + `ai-product-manager-skills` 的阶段路由（SKILL_ROUTING.md）+ `pm-agent-harness-kit` 的流水线角色（pm-lead.md）。
> 最后更新：2026-09-10 <!-- [MA 2026-09-10] S021 提炼：回应"固定流程化环节建流水线、灵活环节交 Pi/用户"的方向校准 -->

## 一、参考库对职能主线的原始概括

### 1.1 pm-agent-harness-kit — 流水线四角色（pm-lead.md）

```
pm-explorer（探索）→ pm-strategist（策略，条件性）→ pm-builder（构建）→ pm-reviewer（评审）
```
- **Explorer**：问题理解、用户研究、竞品情报、市场上下文
- **Strategist**（仅战略级动议）：定位、市场规模、路线图影响
- **Builder**：PRD、用户故事、故事地图等工程就绪产物
- **Reviewer**：证据质量、指标就绪度、实验设计的验收打回

轻量模式（问答/建议/查公式）不跑流水线，直接答。

### 1.2 ai-product-manager-skills — 阶段路由（SKILL_ROUTING.md）

| 阶段 | 首选技能 | 停止/转交条件 |
|---|---|---|
| 问题和目标含糊 | 校准 | 问题可研究或可设计后转下游 |
| 系统产品研究/竞品证据 | research | 单读者学习用 learning-report；跨职能扫描用 Dashboard；取舍交 decision |
| 需要明确选择 | decision-research | 有推荐+排除理由+置信度+颠覆条件 |
| 方案尚未成形 | brainstorming | 用户确认 Design Spec 后交 PRD 或 UI |
| 方案已成形但担心失败 | grill-me（红队质询） | 返回最小 Challenge/Design Delta，不自行放行 |
| 需要正式 PRD | prd-architect | 产出 PRD + 可选 Delivery Manifest，停止在 review_pending |
| 需要 UI 结构/高保真 | ui-mockup | structure-only 可在结构确认后停止 |
| 已有 PRD 需验收 | prd-review | ready 才能拆 issues 或发布 |
| ready PRD 需拆单 | prd-to-issues | 先 draft；版本切片/发布需单独确认 |

**两个 Workflow**：`problem-to-solution`（校准→研究→brainstorm→方案确认）、`solution-to-delivery`（PRD→UI→评审→拆单→交付评审→打包）。
**三个 Loop**（各有三轮上限、两轮无有效差量即停）：decision-loop、solution-loop、delivery-loop。

### 1.3 Product-Manager-Skills — 技能三分法（skills-by-type.md）

| 类型 | 数量 | 特征 | 例子 |
|---|---|---|---|
| **Workflow**（多步骤流程） | 20 | 有阶段步骤、输入输出契约、推进项目主体 | discovery-process、prd-development、roadmap-planning、eol-process、product-strategy-session、competitive-analysis-process |
| **Component**（结构化产出） | 28 | 单产物、有模板、可直接消费 | user-story、problem-statement、press-release、stakeholder-mapping、jobs-to-be-done |
| **Interactive**（交互建议） | 29 | 需要对话、判断、取舍建议 | prioritization-advisor、positioning-workshop、lean-ux-canvas、eol-readiness-advisor |

## 二、固定流程化环节（建确定性流水线的候选）

> 判断标准：①有明确阶段步骤顺序；②有输入/输出契约；③有 Gate/HITL 点；④每次迭代/发布/需求都要做（推进项目主体进度）。

| 主线阶段 | 候选职能 | 固定步骤 | 输入 | 输出 | Gate/HITL 点 | 参考源 |
|---|---|---|---|---|---|---|
| 探索→方案 | **PRD 生成**（已建成） | intake→需求发现→需求确认门→PRD 生成→落盘 | 需求描述 | prd.md + insights.md | 需求确认门（三色表） | 10x/prd、phk/pm-builder |
| 方案验收 | **PRD 评审** | 五维 rubric 打分→blockers 表→三档结论 | PRD | 评审报告 + 修订意见 | 通过/带警告通过/打回 | apm/prd-review、phk/pm-reviewer |
| 需求→交付 | **拆研发工单** | vertical slice→覆盖矩阵→确认后发 issue | PRD | issues 清单 | 确认后才发 | apm/prd-to-issues |
| 迭代规划 | **冲刺计划** | 容量估算→选故事→依赖映射→风险识别→计划摘要 | backlog+velocity | sprint plan | DoR 检查 | pmsk/sprint-plan |
| 交付→发布 | **发布计划** | Tier 分层→T-minus 倒排→灰度→go/no-go→值班 | 版本范围 | launch plan | 二元 go/no-go | 10x/launch-plan |
| 发布 | **发版说明** | 过滤→翻译 feature→outcome→排序→语气→链接 | commits/PRs | release notes | 无（产出即交付） | 10x/release-notes |
| 度量 | **指标树** | 北极星→驱动树→埋点规范 | 业务目标 | metrics tree + 埋点 | 无 | 10x/metrics-tree |
| 实验 | **实验设计** | 假设→护栏指标→样本量→决策规则 | 待验证假设 | experiment plan | 预先承诺决策规则 | 10x/experiment-design |
| 复盘 | **复盘** | 对账→三格式→反馈聚类→行动项→结转 | 迭代/发布结果 | retro report | 无 | pmsk/retro |
| 优先级 | **优先级排序** | 选框架→打分表→约束切分→战略 override→tradeoff memo | backlog+约束 | ranking + memo | 无（产出供决策） | 10x/prioritize |
| 风险 | **事前验尸** | 假设失败→分类 Tiger/Paper/Elephant→分级→行动计划 | PRD/launch plan | pre-mortem | 无 | pmsk/pre-mortem |
| 测试 | **测试场景** | 读 story→目标→前置条件→角色→步骤→预期→边界 | user story | test scenarios | 无 | pmsk/test-scenarios |

## 三、灵活判断环节（交给 Pi 自主模式 + 菜谱技能）

> 特征：需要对话、判断、取舍、创意；没有唯一正确步骤；产出因人因场景而异。

| 类别 | 职能 | 为什么灵活 | 现有菜谱技能 |
|---|---|---|---|
| 探索 | 竞品调研/公司研究 | 搜索范围、深度、角度随问题变 | research-investigation |
| 探索 | 反馈分类 | 分类维度随反馈堆变化 | feedback-triage |
| 策略 | 路线图 | Now/Next/Later 需战略判断 | roadmap |
| 策略 | 定位/价值主张 | 需要创意和取舍 | （无菜谱） |
| 沟通 | 干系人沟通话术 | 受众、语气、措辞随对象变 | stakeholder-comms |
| 沟通 | 高管进展汇报 | 重点取舍随汇报对象变 | （无菜谱） |
| 决策 | 优先级框架选择 | 选 RICE/ICE/机会成本需判断场景 | （无菜谱，10x/prioritize 偏结构化） |
| 探索 | 用户访谈提纲/纪要综合 | 访谈是对话，提纲需临场调整 | （无菜谱） |

## 四、与我们项目现状的对照

### 已建成流水线（确定性状态机）
- ✅ **PRD 生成**：`src/nodes/`（exploration.py + prd.py + hitl.py + artifact.py），端到端跑通

### 仅有菜谱技能（.pi/skills/，未建流水线）
research-investigation、feedback-triage、roadmap、metrics-tree、prd-review、prd-to-issues、stakeholder-comms、experiment-design、launch-plan、retro

### 待建流水线候选（按"项目主体推进"优先级）
1. **PRD 评审**（prd-review）—— PRD 的下游 Gate，无它则 PRD 质量无验收
2. **拆研发工单**（prd-to-issues）—— PRD→开发的桥梁，主体推进环节
3. **发布计划**（launch-plan）—— 每次发布必做，已有菜谱可参考
4. **冲刺计划**（sprint-plan）—— 每个迭代必做
5. **实验设计**（experiment-design）—— AI PM 核心能力，已有菜谱
6. **发版说明**（release-notes）—— 每次发布必做
7. **指标树**（metrics-tree）—— 度量基础
8. **复盘**（retro）—— 迭代闭环

## 五、建流水线的分工原则（用户定调）

> **固定流程化的部分建确定性流水线**（阶段边界、输入输出、Gate 条件写成可机器执行的协议，端到端可测、产出稳定）；
> **判断、实施、创意这些灵活环节交给 Pi 自主模式或寻求用户帮助**（Pi 按需加载已就绪的菜谱技能、调用工具脚本）。
>
> 子阶段（流水线节点）只产出结构化产物，不做跨阶段决策；跨阶段决策和用户确认由 Gate/HITL 点处理。

<!-- [MA 2026-09-10] 提炼自 D:\PM项目参考\ 三个库的职能主线概括，作为建流水线的对照基准 -->
