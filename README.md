# AI PM Agent

给「AI 产品经理」这个职业做的工作流助手：重活走一条确定性流水线（接需求 → 需求分析 → 写方案 → 评审 → 设计评测 → 对比选型 → 拆工单 → 构建期评测 → 发布计划 → 落盘），轻活由常驻 Agent 直接做。产物一律 Markdown。

- 流水线：LangGraph 确定性状态图，12 段 20 节点，819 个测试全绿
- 双轨：重活走流水线，轻活走 Pi 内核 + PM 技能（`.pi/skills/`）
- 单 Agent，不做多 Agent 管道

## 仓库里有什么（= AI 本体）

```
src/            流水线内核（节点 / 判定 / 状态机 / 提示词 / 校验）
tests/          819 个测试
scripts/        入口脚本（run_prd_workflow 等）与工具脚本
artifacts/      产物模板
.pi/skills/     内置 Agent 技能（10 个 PM 技能 + 操作手册）
references/     项目自制参考（外置工具路由、模型候选清单、流水线拓扑等）
docs/           三份核心规格：PRD.md、technical-design.md、workflow-design.md
工具与参考/文字改写/   自制文字改写技能（humanize-writing）
```

这个仓库**只有 AI 本体**。会话记忆、任务书、调研草稿、历史报告等开发过程文档不上仓库（留在本地）。

## 用之前要先连接什么（依赖都在外面）

运行时依赖两类：**模型**和**外部工具**。仓库不内置任何密钥与工具实体，跑起来之前按下面接。

### 1. Python 环境 + 模型

```bash
pip install -r requirements.txt        # 见 requirements.txt
cp .env.example .env                    # 填模型密钥
```

模型通过 LiteLLM 统一层接入，`config.yaml` 的 `llm.providers` 配置：
默认 `DEEPSEEK_API_KEY`（deepseek/deepseek-v4-flash）；也支持 Anthropic / Gemini / Ollama 本地模型，按模板取消注释即可。密钥只走环境变量，不硬编码。

### 2. 外部工具（按需连接，走路由表）

流水线只负责「判断」，具体执行用外面现成的工具。每个工具用前要装好、并在 `references/外置工具路由.md` 登记路径。项目通过 `config.yaml` 引用它们的路径。

| 用途 | 工具 | 仓库引用点 | 需要装在哪 |
|---|---|---|---|
| 评测 / 对比选型跑分 | Promptfoo | `config.yaml` → `eval_tools.promptfoo_dir`、`bake_off.candidates` | `工具与参考/Agent外置工具包/AI评测工具包/tools/promptfoo` |
| 画流程图 / 架构图 | mermaid-cli | `references/外置工具路由.md` | `工具与参考/Agent外置工具包/演示工具包/` |
| 演示 / PPT | 演示工具包（frontend-slides / lieflat-charts / ppt-master） | `references/外置工具路由.md` | 同上 |
| 领域知识库检索 | ChromaDB + 硅基流动嵌入 | `config.yaml` → `domain_kb` | 本地 `domain_kb/`（建库后才有） |

> 路径约定：`工具与参考/Agent外置工具包/` 目录被 .gitignore 排除、不进仓库；`工具与参考/PM项目参考/`（第三方参考库）同样在仓库外。克隆本仓库后，按上表把工具装到对应位置即可。

### 3. 一次性数据依赖（可重建）

| 数据 | 位置 | 说明 |
|---|---|---|
| 领域知识库 | `domain_kb/` | 由 `scripts/rag_ingest.py` 从源文档建库，缺了不阻塞主流程 |
| 业务档案库 | `kb_store/` | 空库可运行 |
| 状态库 | `ai_pm_agent.db` | 运行时自动生成 |
| 产出 | `output/` | 运行时自动生成 |

## 怎么跑

重活（整条流水线）：

```bash
python scripts/run_prd_workflow.py --requirement "你的需求描述"
# 跑到停下来等人确认的地方会打印 STATUS: HITL，你答复后继续
# 中断后可从上次停处续跑：--resume
```

轻活（常驻 Agent）：按 `AGENTS.md` 和 `.pi/skills/agent-manual/` 操作手册使用。

测试：`pytest tests/ -q`（全部本地假 LLM，不需要真实 API）。

## 一句话架构

**判断内嵌、执行外置、产物 Markdown、外置是引用**——需要「过不过 / 推不推荐」的判断由代码硬算（可复现可测试），跑评测、画图、做演示这类动作交给外面现成的工具，工具通过路由表登记、按段调用，只传数据进出。
