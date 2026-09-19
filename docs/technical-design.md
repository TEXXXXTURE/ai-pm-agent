# AI PM Agent — 技术设计方案

> 版本：v1.0
> 日期：2026-09-08
> 状态：M1-M10 已全部建成（截至 S029 共 164 测试全绿——现已达 819 全绿、多段真机验收）；本文的机制设计（三件套、NodeRunner、HITL、组件架构）仍有效
> 前置文档：[workflow-design.md](workflow-design.md)（节点级设计）、[PRD.md](PRD.md)（产品需求）
> 核心问题：**Agent 如何按既定通路"思考"——通路不被模型自由发挥带偏，节点内有结构化推理，HITL 点能中断恢复**

> ⚠️ **时效声明（S031，2026-09-12）**：本文第二节 State、第五节 G1-G9 映射、第六节图装配、第七节 M1-M10 拆解说的是 S007-S008 开建时的规划形态，与当前代码已有差异（实际 11 节点三门、Markdown 产物、评审/工单/发布计划三条流水线建成）。**工作流顺序、节点规划与新增门以 [workflow-design.md v3.0](workflow-design.md)（AI PM 融合工作流 12 段）为准**；实现新节点时按 v3.0 第八节实施队列分块更新本文对应章节，不再整体回写。 <!-- [MA 2026-09-12] -->

---

## 一、核心机制：三件套

Agent 按通路思考，靠三个机制保证：

| 机制 | 解决什么 | 技术载体 |
|---|---|---|
| **通路 = 确定性状态图** | 走哪条路不由模型决定，由 State 字段 + 条件边决定 | LangGraph StateGraph |
| **思考 = 节点内统一执行器** | 每个节点的"推理"走同一模式：上下文装配→结构化输出→校验自检→写回 State | NodeRunner + Pydantic + 可插拔组件 |
| **HITL = 原生中断 + 持久化** | 人机门暂停等人决策，进程退出可断点续跑 | LangGraph interrupt() + SQLite Checkpointer |

### 1.1 通路 = 确定性状态图

**核心原则**：模型不决定路由走向。

```python
# 条件边示例：路由权在 State 字段，不在模型
def after_strategy_decision(state: PMState) -> str:
    if state["proceed_decision"]:
        return "section_planning"
    else:
        return "__end__"

graph.add_conditional_edges(
    "strategy_decision",
    after_strategy_decision,  # 纯函数，零模型调用
)
```

所有条件边都是纯函数读 State 字段做判断：

| 条件边 | 判断字段 | 走向 |
|---|---|---|
| `requirement_confirm` → | `proceed_decision` | True→`needs_discovery`；False→`__end__` |
| `strategy_decision` → | `proceed_decision` | True→`section_planning`；False→`__end__` |
| `red_team_review` → | `red_team_review.has_changes` | False→`model_selection`；True→`prd_revision` |
| `prd_revision` → | `prd_revision_count` | <3→`red_team_review`（循环）；≥3→`__end__`（上报用户） |

**意义**：通路是 100% 确定性的。模型在节点内部做推理和产出，但"接下来走哪个节点"永远不由模型说了算。这保证工作流永远不会被模型自由发挥带偏。

### 1.2 思考 = 节点内统一执行器（NodeRunner）

每个自动节点走**同一个执行模式**，业务逻辑全部外置到可插拔组件：

```
┌─────────────────────────────────────────────────────────┐
│                    NodeRunner.run()                      │
│                                                          │
│  1. 上下文装配                                           │
│     ├─ 读取 State 切片（节点声明需要哪些字段）           │
│     ├─ 知识库检索结果注入（G4：接活先查家底）           │
│     └─ Prompt 模板渲染（Jinja2，从 components/ 加载）    │
│                                                          │
│  2. 调模型 + 结构化输出                                  │
│     ├─ LangChain LLM 调用                               │
│     ├─ Pydantic Model 约束输出格式                       │
│     └─ 校验失败 → 带反馈重试（上限 max_retries 次）     │
│                                                          │
│  3. 节点自检（质量门控）                                 │
│     ├─ 加载该节点的自检规则（components/guards/）        │
│     ├─ 全过 → 继续；不过 → 带反馈回到步骤 2             │
│     └─ 自检也失败 → 标记降级，写回 State 继续            │
│                                                          │
│  4. 写回 State                                           │
│     └─ 按 spec.state_outputs 映射，更新对应字段          │
│                                                          │
│  ※ 仅允许使用声明的工具（tools=None 时纯推理不调工具）   │
└─────────────────────────────────────────────────────────┘
```

**关键设计**：PM 方法论全部固化在三类**可插拔组件**里，内核零业务知识：

| 组件类型 | 载体 | 内容 | 例子 |
|---|---|---|---|
| Prompt 模板 | `components/prompts/*.md` | 节点的推理指令、方法论框架 | `prd_generation.md` 内含章节结构、描述列分块规则、禁止技术接口表述 |
| 输出 Schema | `components/schemas/*.py` | Pydantic 模型，约束模型输出结构 | `MetricsTreeSchema` 要求 North Star + drivers + guardrails + blindspot |
| 自检规则 | `components/guards/*.py` | 产物输出前的质量校验函数 | `no_tech_jargon()` 检查 PRD 描述列无 API/字段/错误码 |

未来桌面端"可视化改动"改的就是这三个目录里的文件——内核一行不动。

### 1.3 HITL = 原生中断 + 持久化

```python
from langgraph.types import interrupt, Command
from langgraph.checkpoint.sqlite import SqliteSaver

# 状态落 SQLite，进程退出可恢复
checkpointer = SqliteSaver.from_conn_string("ai_pm_agent.db")

# 4 个 HITL 节点用 interrupt() 暂停
def requirement_confirm_node(state: PMState) -> PMState:
    # 装配决策材料给用户看
    material = {
        "info_assessment": state["info_completeness"],
        "clarification_questions": generate_clarifications(state),
        "capability_boundary": state["capability_boundary"],  # G8
    }
    # 暂停，等用户输入
    user_response: dict = interrupt(material)
    # 用户恢复后处理
    if user_response["action"] == "confirm":
        state["confirmed_requirement"] = user_response["confirmed_text"]
        state["proceed_decision"] = True
        # G2: 确认后立即建初始评测集
        state["eval_cases"] = init_eval_cases(state)
    elif user_response["action"] == "reject":
        state["proceed_decision"] = False
    return state

# 图编译时指定 interrupt 点
graph = builder.compile(
    checkpointer=checkpointer,
    # interrupt 通过节点内 interrupt() 函数触发，无需 interrupt_before 参数
)

# 运行时按 initiative_id 作为 thread_id，断点续跑
config = {"configurable": {"thread_id": state["initiative_id"]}}
# 进程退出后，下次用同一 thread_id 调 graph.invoke(None, config) 即可恢复
```

**CLI 端 HITL 交互流程**：

```
[Agent] 需求确认门 — 以下是对你需求的理解：
  问题背景：✅ 已提供
  目标：⚠️ 需补充
  用户场景：✅ 已提供
  ...
  
  Agent 能力边界（G8）：
  | 环节 | 自动 | 工具辅助 | 人工 |
  | 需求挖掘 | ✅ | — | — |
  | 竞品数据收集 | — | ✅ 搜索工具 | — |
  | 策略决策 | — | — | ✅ |

  追问 3 个问题：
  1. 这个功能的目标用户是谁？
  2. 现有方案是什么？为什么不满意？
  3. 成功怎么衡量？

  [confirm/reject/supplement] > _

# 用户输入后，Agent 从 interrupt 恢复，继续执行下一节点
```

---

## 二、完整 State Schema（融入 G1-G9）

```python
from typing import TypedDict

class PMState(TypedDict):
    # ─── 标识 ───
    initiative_id: str                    # 全流程唯一标识（兼作 SQLite thread_id）
    requirement_name: str                 # 需求名（文件夹名）
    current_stage: str                    # 当前阶段名（探索/PRD/评估/AI专项）

    # ─── 知识库底座（G4 横切：全程相伴）───
    kb_context: dict                      # 开工前查家底的检索结果（选型/评测/案例）
    kb_written_back: list                 # 本流程已写回知识库的档案 ID 列表

    # ─── 探索阶段 ───
    raw_requirement: str                  # 用户原始需求描述
    info_completeness: dict               # 6 维度信息完整度评估
    confirmed_requirement: str            # 用户确认后的需求
    user_insights: dict                   # 从用户脑中挖出的信息
    external_evidence: dict               # 外部验证证据
    opportunity_score: dict               # 机会评分（ODI/RICE）+ G7: cost_feasibility
    competitor_teardown: dict             # 竞品拆解结果
    ai_feasibility: dict                  # G5: 必须AI做 / 传统就能做 / AI反而更差
    capability_boundary: dict             # G8: 自动/工具/人工 三色表
    component_candidates: list            # G8+G9: 可插拔组件候选 + 开源优先检查结果
    proceed_decision: bool | None         # 是否继续做（用户决策）

    # ─── 评测集（G2 全程生长）───
    eval_cases: list                      # 需求确认门后建立，每个节点可追加用例

    # ─── PRD 阶段 ───
    section_plan: dict                    # 章节裁剪计划（G6: AI功能异常边界默认必出）
    section_confirmed: bool               # 章节裁剪是否经用户确认
    prd_html: str                         # PRD HTML 内容
    prd_sections: list                     # PRD 章节列表
    red_team_review: dict                 # 红队审查反馈（含 has_changes 标志）
    prd_revision_count: int               # PRD 修订次数（上限 3 轮）

    # ─── 评估阶段 ───
    metrics_tree: dict                    # North Star + drivers + guardrails + blindspot
    experiment_design: dict              # 可证伪假设 + 统计功效 + 实验分组
    ai_eval_design: dict                 # eval cases + rubric + 阈值（与 eval_cases 联动）

    # ─── AI 专项 ───
    model_selection: dict                 # G1: 循环化（查档案→bake-off→写回→出结论）
    failure_modes: dict                  # 失败模式分析 + 缓解方案

    # ─── 产物管理 ───
    artifacts: dict                       # 产物文件路径映射
    human_feedback: list                  # 所有人机交互记录

    # ─── 交付后接口（G3 二期留接口）───
    post_launch_hooks: dict | None       # bad case 回收 / 回归重跑 接口占位
```

**与 v1.0 workflow-design.md 的差异**（G1-G9 融入后的新增字段）：

| 新增字段 | 对应缺口 | 说明 |
|---|---|---|
| `kb_context` | G4 | 开工前检索知识库，全程可读 |
| `kb_written_back` | G4 | 交付时写回，闭环 |
| `ai_feasibility` | G5 | 探索阶段判断"该不该用 AI 做" |
| `capability_boundary` | G8 | 需求确认门输出三色表 |
| `component_candidates` | G8+G9 | 组件候选清单 + 开源优先检查 |
| `eval_cases` | G2 | 需求确认后建立，全流程生长 |
| `post_launch_hooks` | G3 | 二期接口占位 |
| `opportunity_score` 增加 `cost_feasibility` | G7 | 成本可行性纳入机会评分 |
| `section_plan` 增加 AI 异常边界默认必出规则 | G6 | PRD 章节裁剪规则增强 |
| `model_selection` 改为循环结构 | G1 | 查档案→bake-off→写回→出结论 |

---

## 三、节点声明（NodeSpec）与执行器（NodeRunner）

### 3.1 NodeSpec — 业务节点薄声明

每个 PM 业务节点只需声明"用哪些可插拔组件"，不写执行逻辑：

```python
@dataclass
class NodeSpec:
    """节点声明 — 业务节点薄到只声明用什么组件"""
    node_id: str                          # 节点 ID（对应 LangGraph 节点名）
    prompt_template: str                  # components/prompts/ 中的文件名
    output_schema: type                   # Pydantic Model（输出结构约束）
    state_inputs: list[str]               # 从 State 读取哪些字段
    state_outputs: dict[str, str]          # 写回 State 的字段映射 {state_key: result_field}
    tools: list[str] | None               # 允许使用的工具名（None=纯推理）
    self_check_rules: list[str]           # 自检规则 ID（components/guards/ 中的函数名）
    max_retries: int = 2                  # schema 校验失败重试上限
```

**示例 — 竞品拆解节点声明**：

```python
competitor_teardown_spec = NodeSpec(
    node_id="competitor_teardown",
    prompt_template="prompts/competitor_teardown.md",
    output_schema=CompetitorTeardownSchema,
    state_inputs=["confirmed_requirement"],
    state_outputs={"competitor_teardown": "result"},
    tools=["web_search", "page_fetch"],    # 需要搜索和抓取网页
    self_check_rules=["all_15_chapters_present", "has_verdict"],
    max_retries=2,
)
```

**示例 — PRD 生成节点声明**：

```python
prd_generation_spec = NodeSpec(
    node_id="prd_generation",
    prompt_template="prompts/prd_generation.md",
    output_schema=PRDOutputSchema,
    state_inputs=["confirmed_requirement", "section_plan", "user_insights", "eval_cases"],
    state_outputs={"prd_html": "html", "prd_sections": "sections"},
    tools=None,                            # 纯推理，不调外部工具
    self_check_rules=["no_tech_jargon", "required_blocks_present", "html_self_contained"],
    max_retries=3,                         # PRD 重要，多给一次重试
)
```

### 3.2 NodeRunner — 统一执行器（内核）

```python
class NodeRunner:
    """统一节点执行器 — 内核零业务知识，所有业务在可插拔组件里"""

    def __init__(self, llm, kb, registry):
        self.llm = llm          # LangChain LLM 实例
        self.kb = kb            # 知识库检索器（G4）
        self.registry = registry  # 组件注册表（自动发现 components/）

    async def run(self, spec: NodeSpec, state: PMState) -> dict:
        """执行单个节点，返回要更新的 State 字段"""
        # ── 1. 上下文装配 ──
        context = self._assemble_context(spec, state)

        # ── 2. 调模型 + 结构化输出（带重试）──
        result = await self._call_model_with_retry(spec, context)

        # ── 3. 节点自检 ──
        check_result = self._run_self_checks(spec, result)
        if not check_result.passed:
            # 自检不过，带反馈再试一次
            context["retry_feedback"] = check_result.feedback
            result = await self._call_model_with_retry(spec, context)

        # ── 4. 写回 State ──
        state_updates = self._map_outputs(spec, result)
        return state_updates

    def _assemble_context(self, spec, state):
        """装配上下文：State 切片 + 知识库检索 + Prompt 模板"""
        ctx = {}
        # 读取声明的 State 字段
        for key in spec.state_inputs:
            ctx[key] = state.get(key)
        # G4: 查家底 — 自动检索知识库相关内容
        ctx["kb_context"] = self.kb.retrieve_relevant(ctx)
        # 加载 Prompt 模板并渲染
        prompt_tmpl = self.registry.load_prompt(spec.prompt_template)
        ctx["rendered_prompt"] = prompt_tmpl.render(**ctx)
        return ctx

    async def _call_model_with_retry(self, spec, context):
        """调模型，Pydantic 结构化输出，失败带反馈重试"""
        last_error = None
        for attempt in range(spec.max_retries + 1):
            try:
                # 如果有 retry_feedback，附加到 prompt
                prompt = context["rendered_prompt"]
                if "retry_feedback" in context:
                    prompt += f"\n\n上次输出有问题：{context['retry_feedback']}\n请修正后重新输出。"
                # 调模型，强制结构化输出
                if spec.tools:
                    # 需要工具的节点：ReAct 模式，但工具集受限
                    result = await self.llm.invoke_with_tools(
                        prompt=prompt,
                        output_model=spec.output_schema,
                        tools=self.registry.load_tools(spec.tools),
                    )
                else:
                    # 纯推理节点
                    result = await self.llm.invoke(
                        prompt=prompt,
                        output_model=spec.output_schema,
                    )
                return result
            except ValidationError as e:
                last_error = str(e)
                context["retry_feedback"] = f"输出格式校验失败：{last_error}"
        # 重试上限耗尽，抛异常或降级
        raise NodeExecutionError(f"{spec.node_id} 重试 {spec.max_retries} 次仍失败: {last_error}")

    def _run_self_checks(self, spec, result):
        """执行节点自检规则"""
        checks = self.registry.load_guards(spec.self_check_rules)
        failures = []
        for check in checks:
            passed, msg = check(result)
            if not passed:
                failures.append(msg)
        if failures:
            return CheckResult(passed=False, feedback="; ".join(failures))
        return CheckResult(passed=True, feedback="")
```

### 3.3 HITL 节点模式（与自动节点不同）

HITL 节点不走 NodeRunner，走独立的中断-恢复模式：

```python
def strategy_decision_node(state: PMState) -> PMState:
    """策略决策门 — HITL 节点"""
    # 装配决策材料（纯 Python，不调模型）
    material = {
        "user_insights": state["user_insights"],
        "external_evidence": state["external_evidence"],
        "opportunity_score": state["opportunity_score"],
        "competitor_teardown": state["competitor_teardown"],
        "ai_feasibility": state["ai_feasibility"],       # G5
        "recommendation": generate_recommendation(state),  # 模型生成建议
    }
    # interrupt 暂停，等待用户决策
    user_response: dict = interrupt(material)
    # 恢复后处理
    state["proceed_decision"] = user_response["proceed"]
    state["human_feedback"].append({
        "node": "strategy_decision",
        "material": material,
        "response": user_response,
    })
    return state
```

### 3.4 节点类型总结

| 类型 | 执行方式 | 模型角色 | 工具 | 例子 |
|---|---|---|---|---|
| **纯推理节点** | NodeRunner + tools=None | 生成结构化产出 | 无 | PRD 生成、指标树设计 |
| **工具辅助节点** | NodeRunner + tools=[...] | 自主调用受限工具集 | 搜索/抓取/检索 | 竞品拆解、外部验证 |
| **HITL 节点** | interrupt() + 恢复 | 生成决策材料/建议 | 无 | 需求确认门、红队审查 |
| **副作用节点** | 直接操作文件系统/知识库 | 无 | 文件读写/KB写回 | 产物持久化、知识库写回 |
| **子图节点** | 调用子 LangGraph | 子图内循环 | 子图内定义 | 模型选型循环（G1） |

---

## 四、可插拔组件架构

### 4.1 文件式组件 + 自动发现

```
ai_pm_agent/
├── components/
│   ├── prompts/               # Prompt 模板（.md，Jinja2 语法）
│   │   ├── intake.md
│   │   ├── needs_discovery.md
│   │   ├── competitor_teardown.md
│   │   ├── prd_generation.md
│   │   ├── red_team_review.md
│   │   ├── metrics_tree.md
│   │   ├── model_selection.md
│   │   └── ...
│   ├── schemas/               # 输出模型（.py，Pydantic Model）
│   │   ├── intake.py
│   │   ├── needs_discovery.py
│   │   ├── competitor_teardown.py
│   │   ├── prd_generation.py
│   │   └── ...
│   ├── tools/                 # 工具（.py，@tool 装饰器）
│   │   ├── web_search.py
│   │   ├── page_fetch.py
│   │   ├── kb_retrieve.py     # G4: 知识库检索工具
│   │   └── ...
│   ├── guards/                # 自检规则（.py，返回 (bool, str) 的函数）
│   │   ├── prd_checks.py
│   │   ├── metrics_checks.py
│   │   └── ...
│   └── registry.py            # 自动发现 + 注册表
│
├── nodes/                     # PM 业务节点声明（薄声明）
│   ├── exploration.py         # 探索阶段节点 spec 集合
│   ├── prd.py                 # PRD 阶段节点 spec 集合
│   ├── evaluation.py          # 评估阶段节点 spec 集合
│   └── ai_special.py          # AI 专项节点 spec 集合
│
├── kernel/                    # 最简内核（零业务知识）
│   ├── state.py              # PMState 定义
│   ├── runner.py             # NodeRunner
│   ├── graph.py               # LangGraph 图装配（节点+边+条件边）
│   ├── hitl.py                # HITL 中断/恢复封装
│   └── checkpointer.py       # SQLite 持久化
│
├── kb/                        # 知识库（G4）
│   ├── store.py               # 本地检索（INDEX 路由 + 关键词匹配）
│   ├── writer.py              # 写回档案（原子覆盖 + 日期版本）
│   └── feishu_sync.py         # 飞书源同步（前置依赖：飞书授权）
│
├── artifacts/                 # 产物模板
│   ├── templates/             # Jinja2 HTML 模板
│   │   ├── prd.html.j2
│   │   ├── teardown.html.j2
│   │   └── ...
│   └── assets/                # 内嵌 CSS/JS（编辑/保存/复制）
│
├── cli/                       # CLI 前端
│   ├── main.py                # 入口
│   ├── hitl_cli.py             # HITL 交互（打印材料/接收输入）
│   └── output.py              # 产物打开/保存通知
│
└── config.yaml                # 全局配置（模型 Provider/KB 路径等）
```

### 4.2 组件注册表 — 自动发现

```python
# components/registry.py

class ComponentRegistry:
    """自动发现和加载 components/ 目录下的可插拔组件"""

    def __init__(self, components_dir: str):
        self.components_dir = Path(components_dir)
        self._prompts = {}    # name → Jinja2 Template
        self._schemas = {}     # name → Pydantic Model
        self._tools = {}      # name → LangChain Tool
        self._guards = {}     # name → callable
        self._scan()

    def _scan(self):
        """扫描目录，自动注册"""
        # 扫描 prompts/*.md
        for f in (self.components_dir / "prompts").glob("*.md"):
            self._prompts[f.stem] = Jinja2Template(f.read_text())
        # 扫描 schemas/*.py（import + 反射）
        for f in (self.components_dir / "schemas").glob("*.py"):
            self._import_and_register(f, base_class=BaseModel)
        # 扫描 tools/*.py（@tool 装饰器）
        for f in (self.components_dir / "tools").glob("*.py"):
            self._import_and_register(f, decorator=tool)
        # 扫描 guards/*.py（以 check_ 开头的函数）
        for f in (self.components_dir / "guards").glob("*.py"):
            self._import_and_register(f, prefix="check_")

    def load_prompt(self, name: str) -> Jinja2Template:
        return self._prompts[name]

    def load_tools(self, names: list[str]) -> list:
        return [self._tools[n] for n in names]

    def load_guards(self, names: list[str]) -> list:
        return [self._guards[n] for n in names]
```

### 4.3 未来可视化改动路径

```
当前首版                        未来桌面端
─────────                      ─────────
components/                    components/（同一份文件）
  prompts/intake.md             ↑ 桌面 UI 读写同一文件
  schemas/intake.py             ↑ 表单化编辑（JSON Schema → 表单）
  tools/web_search.py           ↑ 开关 + 参数配置
  guards/prd_checks.py          ↑ 规则编辑器
    ↑                                ↑
CLI 读写文件                    桌面 UI 读写文件
    ↑                                ↑
NodeRunner 启动时扫描加载       同一进程，改完即生效
```

**同进程信息流的落地**：首版用文件式组件，CLI 直接读写 `components/` 目录。未来桌面端也是读写同一份文件，区别只是 UI 层从 CLI 换成桌面窗口，内核和组件文件完全不变。

---

## 五、G1-G9 技术落地映射

| 缺口 | 技术落地 | 落在哪个节点/机制 | 实现方式 |
|---|---|---|---|
| **G1 选型循环化** | `model_selection` 节点内子图：查 KB 评测档案 → 无/过期则 bake-off 子流程 → 写回 KB → 出结论 | AI 专项阶段 | 子 LangGraph 图，State 字段 `model_selection` 持循环结果 |
| **G2 考题前置** | `requirement_confirm` HITL 恢复后调 `init_eval_cases()` 初始化 `eval_cases` | 需求确认门 | `eval_cases` 在 State 中全程可见，后续节点可追加 |
| **G3 上线后回流** | `post_launch_hooks` 字段占位 + `artifact_persist` 节点末尾预留接口 | 产物持久化后 | 二期实现 bad_case→eval_cases 回流 + 模型换代回归重跑 |
| **G4 知识库底座** | NodeRunner `_assemble_context()` 自动检索 KB + `artifact_persist` 节点调 `kb.writer.write_back()` | 全程横切 | KB 检索在 NodeRunner 内，对业务节点透明 |
| **G5 AI 可行性** | 探索阶段 `external_validation` 节点输出 `ai_feasibility` 字段 | 外部验证后 | Prompt 模板内含判断框架（必须AI/传统即可/AI更差） |
| **G6 失败兜底进 PRD** | `section_planning` 节点的 Prompt 规则：AI 功能"异常与边界"章默认必出 | 章节裁剪规划 | Prompt 模板中硬编码规则 + 自检 guard 校验 |
| **G7 成本入评分** | `external_validation` 节点的 `opportunity_score` schema 增加 `cost_feasibility` | 外部验证 | Schema 字段约束 + Prompt 引导算成本账 |
| **G8 能力边界评估** | `requirement_confirm` HITL 节点输出 `capability_boundary` 三色表 + `component_candidates` | 需求确认门 | HITL 节点生成 + 用户确认；组件候选 = 能力缺口的工具化载体 |
| **G9 造轮子检查** | `component_candidates` 生成时先查 GitHub/MCP 生态 | 同 G8 节点 | 工具辅助：调搜索工具查开源仓库 |

---

## 六、图装配（LangGraph StateGraph）

### 6.1 完整节点注册

```python
# kernel/graph.py

from langgraph.graph import StateGraph, END

def build_graph(runner: NodeRunner, registry: ComponentRegistry):
    builder = StateGraph(PMState)

    # ── 探索阶段 ──
    builder.add_node("intake", make_node_fn(runner, "intake"))
    builder.add_node("requirement_confirm", requirement_confirm_node)  # HITL
    builder.add_node("needs_discovery", make_node_fn(runner, "needs_discovery"))
    builder.add_node("external_validation", make_node_fn(runner, "external_validation"))
    builder.add_node("ai_feasibility_check", make_node_fn(runner, "ai_feasibility_check"))  # G5
    builder.add_node("competitor_teardown", make_node_fn(runner, "competitor_teardown"))
    builder.add_node("capability_boundary", make_node_fn(runner, "capability_boundary"))     # G8+G9
    builder.add_node("strategy_decision", strategy_decision_node)  # HITL
    builder.add_node("kb_lookup", kb_lookup_node)                   # G4 查家底

    # ── PRD 阶段 ──
    builder.add_node("section_planning", make_node_fn(runner, "section_planning"))
    builder.add_node("section_confirm", section_confirm_node)       # HITL
    builder.add_node("prd_generation", make_node_fn(runner, "prd_generation"))
    builder.add_node("red_team_review", red_team_review_node)       # HITL
    builder.add_node("prd_revision", make_node_fn(runner, "prd_revision"))

    # ── AI 专项 ──
    builder.add_node("model_selection", model_selection_node)       # G1 子图
    builder.add_node("failure_analysis", make_node_fn(runner, "failure_analysis"))

    # ── 评估阶段 ──
    builder.add_node("metrics_tree", make_node_fn(runner, "metrics_tree"))
    builder.add_node("experiment_design", make_node_fn(runner, "experiment_design"))
    builder.add_node("ai_eval_design", make_node_fn(runner, "ai_eval_design"))

    # ── 产物管理 ──
    builder.add_node("artifact_persist", artifact_persist_node)
    builder.add_node("kb_write_back", kb_write_back_node)           # G4 写回

    # ── 边 ──
    # 探索阶段
    builder.set_entry_point("kb_lookup")                             # G4: 先查家底
    builder.add_edge("kb_lookup", "intake")
    builder.add_edge("intake", "requirement_confirm")
    builder.add_conditional_edges(
        "requirement_confirm",
        lambda s: "needs_discovery" if s["proceed_decision"] else END,
    )
    builder.add_edge("needs_discovery", "external_validation")
    builder.add_edge("external_validation", "ai_feasibility_check")  # G5
    builder.add_edge("ai_feasibility_check", "competitor_teardown")
    builder.add_edge("competitor_teardown", "capability_boundary")   # G8+G9
    builder.add_edge("capability_boundary", "strategy_decision")
    builder.add_conditional_edges(
        "strategy_decision",
        lambda s: "section_planning" if s["proceed_decision"] else END,
    )

    # PRD 阶段
    builder.add_edge("section_planning", "section_confirm")
    builder.add_conditional_edges(
        "section_confirm",
        lambda s: "prd_generation" if s["section_confirmed"] else "section_planning",
    )
    builder.add_edge("prd_generation", "red_team_review")
    builder.add_conditional_edges(
        "red_team_review",
        route_after_red_team,  # 见下方
    )
    builder.add_edge("prd_revision", "red_team_review")  # 循环回红队

    # AI 专项 + 评估
    builder.add_edge("red_team_review", "model_selection")  # 无修改时直通
    builder.add_edge("model_selection", "failure_analysis")
    builder.add_edge("failure_analysis", "metrics_tree")
    builder.add_edge("metrics_tree", "experiment_design")
    builder.add_edge("experiment_design", "ai_eval_design")

    # 产物
    builder.add_edge("ai_eval_design", "artifact_persist")
    builder.add_edge("artifact_persist", "kb_write_back")  # G4 写回
    builder.add_edge("kb_write_back", END)

    return builder.compile(checkpointer=checkpointer)


def route_after_red_team(state: PMState) -> str:
    """红队审查后的路由：有修改→修订；无修改→继续；超3轮→END上报"""
    review = state["red_team_review"]
    count = state["prd_revision_count"]
    if review.get("has_changes") and count < 3:
        return "prd_revision"
    elif review.get("has_changes") and count >= 3:
        return END  # 上报用户决策
    else:
        return "model_selection"
```

### 6.2 G1 模型选型子图

```python
# nodes/ai_special.py

from langgraph.graph import StateGraph

def build_model_selection_subgraph(kb, llm):
    """G1: 选型循环化 — 查档案→bake-off→写回→出结论"""
    sub = StateGraph(dict)

    sub.add_node("query_kb", lambda s: {"kb_eval": kb.query_eval(s["requirement"])})
    sub.add_node("check_freshness", check_freshness_node)
    sub.add_node("bake_off", bake_off_node)      # 用 eval_cases 跑对比
    sub.add_node("write_back", lambda s: kb.write_back(s["bake_off_result"]))
    sub.add_node("conclude", conclude_node)       # 汇总结论

    sub.set_entry_point("query_kb")
    sub.add_conditional_edges(
        "check_freshness",
        lambda s: "bake_off" if s.get("needs_bake_off") else "conclude",
    )
    sub.add_edge("bake_off", "write_back")
    sub.add_edge("write_back", "conclude")
    sub.add_edge("conclude", END)

    return sub.compile()


def model_selection_node(state: PMState) -> dict:
    """G1: 调用选型子图"""
    subgraph = build_model_selection_subgraph(state["_kb"], state["_llm"])
    result = subgraph.invoke({
        "requirement": state["confirmed_requirement"],
        "eval_cases": state["eval_cases"],        # G2: 用全程生长的考题
    })
    return {"model_selection": result}
```

---

## 七、首版纵切范围与开发顺序

### 7.1 最小闭环纵切（首版开发目标）

一次打通四大主干：**状态机 / HITL 中断恢复 / 结构化输出 / 产物持久化**

```
kb_lookup → intake → requirement_confirm(HITL) → needs_discovery → prd_generation → artifact_persist
```

| 节点 | 验证什么主干 |
|---|---|
| `kb_lookup` | G4 知识库检索接通（首版可用空库 mock） |
| `intake` | NodeRunner 跑通：Prompt 渲染 + Pydantic 输出 + 写回 State |
| `requirement_confirm` | **HITL interrupt/resume 跑通**；G8 能力边界输出；G2 考题集初始化 |
| `needs_discovery` | 纯推理节点 NodeRunner 验证（无工具调用） |
| `prd_generation` | HTML 产物落盘跑通；自检规则跑通（no_tech_jargon） |
| `artifact_persist` | 按需求名分文件夹保存 HTML |

### 7.2 开发模块拆解（可验收任务清单）

> 每个模块遵循协作工作流：coding 实现 → check 验收 → 通过后进入下一模块。

---

#### M1: 内核骨架

**目标**：搭建 LangGraph 最简内核，State 定义 + NodeRunner + 图装配 + SQLite 持久化 + 组件注册表骨架。

**涉及文件**：
- `kernel/state.py` — PMState TypedDict 定义（完整 G1-G9 字段）
- `kernel/runner.py` — NodeRunner 类（run / _assemble_context / _call_model_with_retry / _run_self_checks / _map_outputs）
- `kernel/graph.py` — build_graph() 函数（节点注册 + 边 + 条件边，首版只注册纵切 6 节点）
- `kernel/checkpointer.py` — SqliteSaver 初始化封装
- `kernel/exceptions.py` — NodeExecutionError / CheckResult 等异常和结果类
- `kernel/spec.py` — NodeSpec dataclass

**实现要点**：
1. PMState 包含 v2.0 全部字段（见 workflow-design.md 第二节）
2. NodeRunner 的 `_assemble_context` 中 KB 检索接口预留，首版传空实现（`kb=None` 时跳过检索）
3. NodeRunner 的 `_call_model_with_retry` 首版用 mock LLM（返回固定 JSON），验证重试逻辑
4. `build_graph()` 首版只注册纵切 6 节点的边，后续模块逐步补全
5. SQLite Checkpointer 可指定 db_path，支持 thread_id 断点恢复

**验收标准**：
- [ ] PMState 实例化后所有字段有默认值或 None
- [ ] NodeRunner.run() 传入 mock spec + mock state，返回正确的 state_updates 字典
- [ ] schema 校验失败时触发重试，达到 max_retries 后抛 NodeExecutionError
- [ ] 自检规则失败时带 feedback 重试一次
- [ ] build_graph() 返回的 compiled graph 可 invoke，不报节点缺失错误（纵切 6 节点已注册）
- [ ] SQLite 持久化：invoke 后 db 文件中存在对应 thread_id 的 checkpoint

---

#### M2: 组件框架

**目标**：components/ 目录结构 + 自动发现注册表 + 首批组件（纵切节点需要的 prompt/schema/guard）。

**涉及文件**：
- `components/registry.py` — ComponentRegistry 类（_scan / load_prompt / load_tools / load_guards）
- `components/prompts/intake.md` — 需求接收 prompt 模板（Jinja2）
- `components/prompts/needs_discovery.md` — 需求挖掘 prompt 模板
- `components/prompts/prd_generation.md` — PRD 生成 prompt 模板（含章节结构+描述列分块规则+禁止技术接口表述）
- `components/schemas/intake.py` — InfoCompletenessSchema（6 维度）
- `components/schemas/needs_discovery.py` — UserInsightsSchema
- `components/schemas/prd_generation.py` — PRDOutputSchema（html + sections）
- `components/guards/prd_checks.py` — no_tech_jargon() / required_blocks_present() / html_self_contained()

**实现要点**：
1. ComponentRegistry._scan() 扫描 4 个子目录（prompts/schemas/tools/guards），自动注册
2. Prompt 模板用 Jinja2 语法，可接收 State 字段渲染
3. Pydantic Schema 定义节点输出结构，字段名与 State 输出映射对齐
4. Guard 函数签名统一：`(result) -> tuple[bool, str]`
5. 首批只做纵切 3 个自动节点（intake / needs_discovery / prd_generation）的组件

**验收标准**：
- [ ] ComponentRegistry 初始化后，_prompts/_schemas/_guards 字典中包含已扫描到的组件
- [ ] load_prompt("intake") 返回可渲染的 Jinja2 Template
- [ ] load_guards(["no_tech_jargon"]) 返回可调用函数
- [ ] intake.md 渲染后包含 State 传入的字段值
- [ ] PRDOutputSchema 实例化时校验 html 非空、sections 是 list
- [ ] no_tech_jargon() 对含 "API" 的文本返回 (False, msg)，对正常文本返回 (True, "")

---

#### M3: CLI 前端

**目标**：命令行入口，支持启动 Agent、多轮对话、流式输出、HITL 交互（打印材料/接收输入/恢复执行）。

**涉及文件**：
- `cli/main.py` — 入口（argparse 解析需求名/阶段/配置路径）
- `cli/hitl_cli.py` — HITL 交互（打印决策材料、接收用户输入、调 graph resume）
- `cli/output.py` — 产物打开/保存通知
- `config.yaml` — 全局配置

**实现要点**：
1. 启动命令：`python -m cli.main --requirement "需求描述" --name "需求名"`
2. HITL 交互：检测到 interrupt 时，用 rich 打印决策材料 → 等待用户输入 → 用 Command(resume=...) 恢复
3. 流式输出：NodeRunner 执行中打印当前节点名和进度
4. 断点恢复：`--resume <thread_id>` 从 SQLite 恢复上次中断点
5. config.yaml 读取模型配置、KB 路径、db 路径

**验收标准**：
- [ ] `python -m cli.main --requirement "做一个AI客服" --name "ai-customer-service"` 正常启动
- [ ] 执行到 requirement_confirm 节点时，CLI 打印决策材料并暂停等待输入
- [ ] 用户输入 confirm 后，Agent 继续执行下一节点
- [ ] Ctrl+C 退出后，`--resume <thread_id>` 可恢复到中断点
- [ ] config.yaml 中模型/DB/产物路径配置生效

---

#### M4: 产物系统

**目标**：Jinja2 HTML 模板 + 按需求名分文件夹保存 + 自包含 HTML（内嵌 CSS/JS，可编辑/可打印/可复制）。

**涉及文件**：
- `artifacts/templates/prd.html.j2` — PRD HTML 模板
- `artifacts/templates/insights.html.j2` — 需求洞察 HTML 模板
- `artifacts/assets/style.css` — 内嵌 CSS（打印友好、可编辑样式）
- `artifacts/assets/editor.js` — 内嵌 JS（contenteditable + outerHTML 保存 + 一键复制）
- `kernel/artifact.py` — ArtifactManager 类（save / load / 按需求名建文件夹）

**实现要点**：
1. Jinja2 模板渲染时将 CSS 和 JS 内联到 HTML 中（自包含）
2. 文件夹结构：`output/[需求名]/需求文档/[需求名]-PRD.html`
3. HTML 中所有元素 `contenteditable`，保存按钮触发 `outerHTML` 覆盖原文件
4. 一键复制：iframe → base64 图片，三档降级
5. ArtifactManager.save(html, requirement_name, doc_type) 返回文件路径

**验收标准**：
- [ ] PRD HTML 打开后脱机可用（无外部依赖）
- [ ] 浏览器中可直接编辑 PRD 内容（contenteditable 生效）
- [ ] 保存后 outerHTML 覆盖原文件，内容更新
- [ ] 文件夹结构正确：`output/ai-customer-service/需求文档/ai-customer-service-PRD.html`
- [ ] 打印预览样式正常（@media print 生效）

---

#### M5: 知识库接入

**目标**：本地检索（INDEX 路由 + 关键词匹配）+ 写回档案（原子覆盖 + 日期版本）；飞书同步留接口。

**涉及文件**：
- `kb/store.py` — KBStore 类（retrieve_relevant / query_eval / query_case）
- `kb/writer.py` — KBWriter 类（write_back / atomic_overwrite）
- `kb/feishu_sync.py` — 飞书同步接口占位（fetch_from_feishu / push_to_feishu）
- `kb/index.json` — INDEX 路由表（档案类型 → 文件路径映射）

**实现要点**：
1. KBStore.retrieve_relevant(query) 首版用关键词匹配（TfidfVectorizer 或简单 substring），无向量库
2. KBStore 返回空结果时不阻塞流程（返回 `{"status": "empty"}`）
3. KBWriter.write_back(archive) 原子覆盖：写临时文件 → rename 替换
4. 每份档案含日期版本：文件头 `<!-- updated: 2026-09-08 -->`
5. 飞书同步：接口定义但不实现，`raise NotImplementedError("飞书授权未配置")`

**验收标准**：
- [ ] KBStore.retrieve_relevant("AI客服选型") 在空库时返回 `{"status": "empty"}`
- [ ] KBWriter.write_back() 写入的档案含日期版本标记
- [ ] 写回后再次 retrieve_relevant 能检索到刚写入的档案
- [ ] 飞书同步接口调用时抛出 NotImplementedError 且消息包含"飞书授权"
- [ ] INDEX 路由表可正确路由到 decision/eval/case 三类档案

---

#### M5.2: RAG 领域知识库（独立模块，与业务档案库并行）

> **性质**：功能模块开发，独立报批。与 M5 业务档案库底层独立，上层统一入口。
> **前置依赖**：M5（知识库底座模式确立）、S030+ 融合工作流定稿
> **完整实施规划**：见 `references/RAG知识库实施规划.md`

**目标**：搭建 AI Agent 领域知识库 RAG 骨架，支持三层知识（概念/精选论文/追踪论文）的分层加权混合检索，作为 G4 知识库底座的领域知识补充。

**涉及文件**：
- `kb/rag/__init__.py` — 模块入口
- `kb/rag/store.py` — RAGStore：分层加权混合检索
- `kb/rag/ingest.py` — RAGIngest：文档分块 + 嵌入 + 入库
- `kb/rag/splitter.py` — 分块器：section-aware + 整篇 chunk
- `kb/rag/embeddings.py` — 嵌入模型封装：Doubao-embedding API
- `scripts/rag_ingest.py` — 入库 CLI
- `scripts/rag_query.py` — 检索 CLI
- `domain_kb/chroma/` — ChromaDB 数据目录（嵌入式，自动创建）
- `domain_kb/source/` — 原始文档源（concept/ + curated-papers/ + tracked-papers/）

**三层知识结构**：

| 层 | 来源 | 数量 | 权重 | 更新频率 | 分块方式 |
|---|---|------|------|---------|---------|
| 概念解读层 | agentic-ai-knowledge-base | 209 篇，32 分类 | ×2.0 | 每季度手动整篇更新 | Section-aware 分块 |
| 精选论文层 | awesome-llm-agent-papers | 514 篇，10 分类 | ×1.5 | 每月 diff 追加 | 整篇一块 |
| 新论文追踪层 | arXiv API + LLM 过滤 | 约 3000-5000 篇 | ×1.0 | 每周自动增量 | 整篇一块 |

**技术选型**：
- 向量数据库：ChromaDB（嵌入式，零运维，支持 metadata 过滤）
- 嵌入模型：Doubao-embedding（火山引擎 API，中英文混合效果好）
- 检索策略：分层加权混合检索（关键词 0.4 + 向量 0.6，再乘层权重）
- 统一分类体系：~25 个分类，三层通过 `layer` + `category` metadata 对齐

**ChromaDB Collection 设计**：
- 单个 collection：`agent_knowledge`
- 每个 chunk metadata 字段：`id` / `layer` / `category` / `source` / `title` / `curated` / `feishu_ref` / `chunk_index` / `section_title` / `chunking_version` / `source_category`
- Chunk ID 规则：`concept-{hash}-{idx}` / `curated-{arxiv_id}` / `tracked-{arxiv_id}`

**检索完整流程**：
```
查询 → 向量检索捞候选集 → 候选集内关键词打分 → 合并 → 融合分（0.4×关键词 + 0.6×向量）
→ 分层加权（×layer_weight × curated_bonus × feishu_ref_bonus）→ 排序 → Top-K
```

**与业务档案库的关系**：
- 两套库底层独立（KBStore vs RAGStore），各自维护
- 上层通过 `kb_query.py` 扩展 `--domain` 参数统一入口（本期不做，留后续）
- 飞书知识地图作为统一导航层，指向两套知识库的不同条目

**验收标准**：
- [ ] 概念层 209 篇文档可成功入库（section-aware 分块 + 向量嵌入）
- [ ] 精选层 40 篇种子论文可成功入库（整篇 abstract + 向量嵌入）
- [ ] `rag_query.py --query "ReAct 是什么"` 返回 5 条结果，概念层结果排序靠前
- [ ] 按 `--layers concept --category planning` 过滤正常工作
- [ ] 单元测试全绿（splitter / ingest / store 各有 mock 测试）
- [ ] 真机冒烟测试通过（导入两层数据 → 3 个查询验证结果质量）

---

#### M6: 纵切节点实现

**目标**：纵切 6 节点的完整 prompt + schema + guard + node spec，跑通完整最小闭环。

**涉及文件**：
- `nodes/exploration.py` — kb_lookup / intake / needs_discovery 的 NodeSpec
- `nodes/hitl.py` — requirement_confirm 节点（interrupt + G8 能力边界 + G2 考题集初始化）
- `nodes/prd.py` — prd_generation 的 NodeSpec
- `nodes/artifact.py` — artifact_persist 节点（调 ArtifactManager）
- `components/prompts/kb_lookup.md` — 查家底 prompt
- `components/prompts/intake.md`（M2 已建，此处完善内容）
- `components/prompts/needs_discovery.md`（M2 已建，此处完善内容）
- `components/prompts/prd_generation.md`（M2 已建，此处完善内容）
- `components/schemas/kb_lookup.py` — KBContextSchema
- `components/schemas/intake.py`（M2 已建，此处完善字段）
- `components/tools/init_eval_cases.py` — G2: 考题集初始化函数

**实现要点**：
1. kb_lookup 节点：调 KBStore.retrieve_relevant(raw_requirement)，结果写入 state["kb_context"]
2. intake 节点：6 维度评估信息完整度，输出 InfoCompletenessSchema
3. requirement_confirm 节点：interrupt 暂停 → CLI 打印材料（含 G8 三色表）→ 用户确认后调 init_eval_cases() 初始化 eval_cases
4. needs_discovery 节点：纯推理，从 confirmed_requirement 挖掘用户洞察
5. prd_generation 节点：生成 HTML PRD，自检规则跑通（no_tech_jargon / required_blocks / html_self_contained）
6. artifact_persist 节点：调 ArtifactManager.save() 保存 HTML 产物

**验收标准**：
- [ ] 纵切 6 节点能完整跑通：kb_lookup → intake → requirement_confirm(HITL) → needs_discovery → prd_generation → artifact_persist
- [ ] requirement_confirm HITL 中断时，CLI 打印的材料包含 G8 能力边界三色表
- [ ] 用户确认后 eval_cases 非空（G2 考题集初始化）
- [ ] prd_generation 输出的 HTML 通过 no_tech_jargon 自检（描述列无 API/字段/错误码）
- [ ] artifact_persist 保存的 HTML 文件路径与需求名匹配
- [ ] 整条纵切链路在 SQLite 中有完整 checkpoint 记录

---

#### M7: 探索阶段补全

**目标**：补全探索阶段剩余 5 节点（external_validation / ai_feasibility_check / competitor_teardown / capability_boundary / strategy_decision）。

**涉及文件**：
- `nodes/exploration.py` — 补充 5 节点的 NodeSpec
- `nodes/hitl.py` — 补充 strategy_decision 节点
- `components/prompts/external_validation.md` — 含 G7 成本可行性
- `components/prompts/ai_feasibility_check.md` — G5: 必须AI/传统即可/AI更差
- `components/prompts/competitor_teardown.md` — 15 章竞品拆解
- `components/prompts/capability_boundary.md` — G8+G9: 三色表+组件候选+开源检查
- `components/schemas/` — 对应 5 个 Pydantic Model
- `components/tools/web_search.py` — 搜索工具（LangChain @tool）
- `components/tools/page_fetch.py` — 网页抓取工具
- `components/guards/exploration_checks.py` — 探索阶段自检规则

**验收标准**：
- [ ] external_validation 输出 opportunity_score 含 cost_feasibility 字段（G7）
- [ ] ai_feasibility_check 输出明确分类 + 理由（G5 自检通过）
- [ ] competitor_teardown 输出包含 15 章全部章节（自检 all_15_chapters_present 通过）
- [ ] capability_boundary 输出三色表覆盖所有步骤 + component_candidates 含开源检查记录（G8+G9）
- [ ] strategy_decision HITL 中断时展示 ai_feasibility + capability_boundary 汇总
- [ ] 探索阶段全链路跑通：intake → ... → strategy_decision，用户选 No → END

---

#### M8: PRD 阶段补全

**目标**：补全 PRD 阶段剩余 4 节点（section_planning / section_confirm / red_team_review / prd_revision）+ 红队 3 轮循环。

**涉及文件**：
- `nodes/prd.py` — 补充 4 节点的 NodeSpec / HITL 节点
- `components/prompts/section_planning.md` — 含 G6: AI 功能异常边界默认必出规则
- `components/prompts/red_team_review.md` — 三视角审查（用户视角/pre-mortem/VP视角）
- `components/prompts/prd_revision.md` — 根据红队反馈修订
- `components/schemas/` — 对应 4 个 Pydantic Model
- `components/guards/prd_checks.py` — 补充 G6 异常边界章节存在性检查
- `kernel/graph.py` — 更红队循环边 + route_after_red_team 条件边

**验收标准**：
- [ ] section_planning 输出中 AI 功能的"异常与边界"章标记为保留（G6）
- [ ] section_confirm HITL 中断时展示保留/省略章节及原因
- [ ] red_team_review HITL 中断时展示三视角审查结果
- [ ] 红队有修改意见 → prd_revision → 回到 red_team_review（循环验证）
- [ ] prd_revision_count 达到 3 时 → END（上报用户）
- [ ] 红队无修改意见 → 直通 model_selection

---

#### M9: AI 专项 + 评估阶段

**目标**：补全 AI 专项 2 节点 + 评估 3 节点，含 G1 模型选型子图。

**涉及文件**：
- `nodes/ai_special.py` — model_selection（含 G1 子图）+ failure_analysis
- `nodes/evaluation.py` — metrics_tree + experiment_design + ai_eval_design
- `components/prompts/model_selection.md` — G1: 查档案→bake-off→写回→出结论
- `components/prompts/failure_analysis.md` — 失败模式 + 缓解方案
- `components/prompts/metrics_tree.md` — North Star + drivers + guardrails
- `components/prompts/experiment_design.md` — 可证伪假设 + 统计功效
- `components/prompts/ai_eval_design.md` — eval cases + rubric + 阈值
- `components/schemas/` — 对应 5 个 Pydantic Model
- `components/guards/` — 对应自检规则

**验收标准**：
- [ ] model_selection 子图跑通：query_kb → check_freshness → bake_off（或 conclude）→ write_back → conclude
- [ ] model_selection 使用 state["eval_cases"] 作为 bake-off 考题（G2 联动）
- [ ] failure_analysis 每类失败模式有缓解方案（自检通过）
- [ ] metrics_tree North Star 是价值单位非虚荣指标（自检通过）
- [ ] ai_eval_design 的 eval cases 与 state["eval_cases"] 联动（G2）
- [ ] AI 专项 + 评估全链路跑通：model_selection → ... → ai_eval_design

---

#### M10: G1-G9 收尾 + kb_write_back

**目标**：补全 kb_write_back 节点 + G3 交付后接口占位 + 全链路联调。

**涉及文件**：
- `nodes/artifact.py` — 补充 kb_write_back 节点 + G3 post_launch_hooks 占位
- `kernel/graph.py` — 补全 kb_write_back → END 边
- `components/guards/` — 补充 kb_write_back 自检（至少写回 1 份档案）

**验收标准**：
- [ ] kb_write_back 节点调 KBWriter.write_back() 写回至少 1 份档案（G4 闭环）
- [ ] post_launch_hooks 字段占位存在，不阻塞流程（G3 二期接口）
- [ ] 完整 22 节点全链路跑通：kb_lookup → ... → kb_write_back → END
- [ ] SQLite checkpoint 记录完整执行轨迹
- [ ] 所有 HITL 节点中断/恢复正常工作

---

### 7.3 开发顺序与依赖图

```
M1 (内核骨架)
├── M2 (组件框架) ──────────┐
├── M3 (CLI 前端)            │
├── M4 (产物系统)            │
└── M5 (知识库接入)          │
                             ▼
                     M6 (纵切节点) ← M1+M2+M4
                      │
             ┌────────┼────────┐
             ▼        ▼        │
       M7(探索)  M8(PRD)       │
             │        │        │
             └────────┼────────┘
                      ▼
               M9 (AI专项+评估)
                      │
                      ▼
               M10 (G1-G9收尾+全链路)
```

- M1 是一切的基础，M2/M3/M4/M5 可并行开发（均只依赖 M1）
- M6 依赖 M1+M2+M4（需要内核+组件+产物系统）
- M7/M8 依赖 M6（需要纵切跑通后补全同阶段节点）
- M9 依赖 M8（PRD 阶段完成后才能做 AI 专项+评估）
- M10 依赖 M9（全链路最后收尾） <!-- [MA 2026-09-08] -->

### 7.4 依赖清单

```txt
# requirements.txt
langgraph>=0.2
langchain>=0.3
langchain-openai>=0.1       # 国产模型均走 OpenAI 兼容接口，只需这一个
pydantic>=2.0
jinja2>=3.1
rich>=13.0                  # CLI 美化
httpx>=0.27                 # 异步 HTTP（工具用）
beautifulsoup4>=4.12        # 网页抓取解析（竞品拆解用）
chromadb>=0.5               # RAG 领域知识库：向量数据库
```

---

## 八、配置文件

```yaml
# config.yaml
llm:
  default_provider: deepseek

  providers:
    deepseek:
      base_url: "https://api.deepseek.com"
      models:
        - deepseek-v4-flash      # 快速低成本
        - deepseek-v4-pro         # 高性能推理
      api_key_env: "DEEPSEEK_API_KEY"

    hunyuan:
      base_url: "https://api.hunyuan.cloud.tencent.com/v1"
      models:
        - hy3
        - hunyuan-turbos-latest
      api_key_env: "HUNYUAN_API_KEY"

    mimo:
      base_url: "https://tokenhub.tencentmaas.com/v1"
      models:
        - mimo-v2.5-pro
      api_key_env: "TOKENHUB_API_KEY"

    zhipu:
      base_url: "https://open.bigmodel.cn/api/paas/v4"
      models:
        - glm-4
        - glm-4-flash
      api_key_env: "ZHIPU_API_KEY"

    qwen:
      base_url: "https://dashscope.aliyuncs.com/compatible-mode/v1"
      models:
        - qwen-plus
        - qwen-max
      api_key_env: "DASHSCOPE_API_KEY"

    moonshot:
      base_url: "https://api.moonshot.cn/v1"
      models:
        - moonshot-v1-8k
        - moonshot-v1-32k
      api_key_env: "MOONSHOT_API_KEY"

  temperature: 0.3
  max_retries: 2

kb:
  store_path: ./kb_store
  feishu_app_id: ""
  feishu_app_secret: ""

domain_kb:                    # RAG 领域知识库
  chroma_path: ./domain_kb/chroma
  source_path: ./domain_kb/source
  embedding:
    provider: doubao
    model: doubao-embedding
    api_key_env: "ARK_API_KEY"
    api_base: "https://ark.cn-beijing.volces.com/api/v3"
    dimensions: 2560
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

artifacts:
  output_root: ./output
  template_dir: ./artifacts/templates

persistence:
  db_path: ./ai_pm_agent.db

graph:
  max_prd_revisions: 3
  node_timeout: 120
```

---

## 九、关键设计决策总结

| 决策 | 结论 | 理由 |
|---|---|---|
| 路由权归属 | State 字段 + 条件边（纯函数） | 通路 100% 确定性，模型不决定走向 |
| 节点执行模式 | 统一 NodeRunner（上下文→结构化输出→自检→写回） | 内核零业务知识，方法论全在可插拔组件 |
| 组件载体 | 文件式（.md/.py/.yaml）+ 自动发现 | 未来桌面端读写同一份文件，同进程信息流零转换 |
| HITL 实现 | LangGraph interrupt() + SQLite Checkpointer | 原生支持中断恢复 + 断点续跑 |
| 红队循环 | State 计数 + 条件边，上限 3 轮 | 结构简单，超限自动 END 上报 |
| G1 选型循环 | 子 LangGraph 图 | 查档案→bake-off→写回 是独立闭环 |
| G4 知识库 | NodeRunner 内自动检索 + 交付时写回 | 对业务节点透明，横切全程 |
| 知识库架构 | 两套独立（业务档案关键词检索 + 领域知识 RAG 向量检索） | 内容性质不同，技术选型差异化，上层统一入口 |
| 首版纵切 | kb_lookup→intake→requirement_confirm→needs_discovery→prd_generation→artifact_persist | 一次打通四大主干 |

<!-- [MA 2026-09-08] -->
