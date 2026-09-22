# AI PM Agent

[![tests](https://github.com/TEXXXXTURE/ai-pm-agent/actions/workflows/tests.yml/badge.svg)](https://github.com/TEXXXXTURE/ai-pm-agent/actions/workflows/tests.yml)
[![Python](https://img.shields.io/badge/Python-3.11%2B-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-MIT-green)](LICENSE)

**一句需求丢进去，能进评审的 PRD、评审结论、研发工单、上线计划出来。**

给产品经理用的工作流助手。它按固定顺序走完整套产品流程，每走完一步，交给你一份 Markdown 文档。写代码、修 bug、搭工程不归它管。

```
接需求 → 需求分析 → 写 PRD → 评审 → 设计评测 → 对比选型 → 拆工单 → 构建期评测 → 发布计划 → 写成文件
```

**20 个节点 · 8 处停下来等你 · 819 个测试 · 测试代码 1.56 万行，比源码（1.16 万行）还多**

## 这个项目做了什么

**一条能从头跑到尾的流水线。** 一句需求进去，一路产出洞察、边界判定、PRD、评审结论、评测体系、选型结论、研发工单和发布计划。13 个节点代码文件、13 份提示词、9 套产物模板、11 个 Agent 技能。

**结论由代码算，不由模型说。** 评审过不过、评测达不达标、就绪度打几档，模型只负责出分数和 JSON，结论由代码按数值算出来。这是「换模型不影响结论、同一份输入跑两遍结果一样」的基础。

**819 个测试，且不花一分钱 API 费。** 全部跑本地假模型，进 CI，每次提交都验一遍。

**每一项技术选择都能指到业界官方文档。** 12 项对位（见下），不靠自创词撑场面——同行照着链接就能核对。

## 技术选型：每一项都对到业界标准

| 用在哪 | 用了什么 | 业界对应 |
|---|---|---|
| 流水线骨架 | LangGraph，20 节点状态机 | Anthropic 五大工作流里的 prompt chaining；LangGraph 官方主推形态 |
| 能力怎么组织 | 14 项职能标准 + 固定流水线 | 业界两种范式（技能库 / 固定流程）的交集 |
| 结论怎么出 | 代码按数值算，模型只给分 | Anthropic「hook not prompt」原则，且执行得更严 |
| 该人拍板的地方 | 8 处暂停/恢复 | LangGraph interrupt，四大框架收敛的同一模式 |
| 跑评测 | Promptfoo，考题先于开发 | EDD（Eval-Driven Development） |
| 模型接入 | LiteLLM 统一模型层 | LLM gateway 的事实标准 |
| 模型选型 | 候选池前置到 PRD 之前 | requirements-first + 候选池 + 自家数据实测 |
| 输出校验 | Structured Outputs + Pydantic | OpenAI / Anthropic 官方推荐路径 |
| 记忆 | 检查点 + 产物写成文件 | LangGraph checkpoint/store + Anthropic Memory tool |
| 工具调用 | 路由表登记 | MCP 的工具目录（tools/list） |
| 长期项目记忆 | 索引 + 内容仓两层 | 比语义检索更可控的一版简化 |
| AI 需求评审看什么 | 数据底座 / 幻觉风险 / 内容安全 / 成本 | 业界五维共识的子集 |

完整对照附 39 条权威来源，见 `docs/技术选型与架构对位报告.md`。

## 自己定的部分在业务层，不在架构层

架构层没有创新，这是刻意的：每个选择都踩在官方文档上，经得起同行看。真正自己定的是三条：

**不替用户做决定。** 业界多数 AI 工具是「替你做完再让你确认」；这里是反过来的——8 个确认点只挡明显不合格的，空答复就继续等，明确答复按字面执行。

**AI 给候选与判据，人拍板。** 选型、评审、评测全按这条走。AI 是产品经理，不是决策者。

**判断留内核，执行放外面。** 什么能放外面（执行、内容）、什么必须留在代码里（判定、契约），定成了规则。工具可以随时升级换代，结论稳在代码里不动。

## 跑一遍是什么样

```bash
PYTHONPATH=src python scripts/run_prd_workflow.py \
  --requirement "客服回复建议助手：客服与用户聊天时，AI 根据上下文生成 1-3 条回复建议供一键采用；识别到情绪激烈或涉及投诉赔付时不给建议，提示转人工组长。" \
  --name 客服回复建议助手
```

`--requirement` 是原始需求，`--name` 是产物文件夹名。跑起来它先复述一遍需求、给一个「这算不算 AI 需求」的判断，然后停下来等你回话：

```
STATUS: HITL
THREAD_ID: 4461143c-6ed5-4de8-9e51-981a3c45312e
NODE: requirement_confirm
QUESTION: 节点「requirement_confirm」进入需求确认门。请审阅下方需求理解与 AI 适用性分流建议，确认无误后回复 confirmed，或回复「非AI」改判普通轨、「AI核心」改判 AI 全轨；其他文本作为需求修订意见处理（分流沿用模型建议，不二次中断）。
---
[中断载荷]
requirement_name: 客服回复建议助手
raw_requirement: 客服回复建议助手：客服与用户聊天时，AI 根据上下文生成 1-3 条回复建议……
（下面接着是需求信息完整度打分、AI 适用性判断）
---
END HITL
```

你回一句「确认」（或 `confirmed`），它接着往下走。全程有 8 个地方会这样停下来等你。跑完产物是文件：

```
output/客服回复建议助手/
├── 需求文档/   客服回复建议助手-prd.md
├── 需求洞察/   客服回复建议助手-insights.md
├── review/     客服回复建议助手-review.md      五维评分 + AI 维度专项检查
├── 研发工单/   客服回复建议助手-issues.md      按「用户能做完一件事」切的工单
├── 发布计划/   客服回复建议助手-launch_plan.md  7 步发布计划
└── 评测/       eval_report.md、bakeoff_report.md、results.json
```

跑到一半想歇会儿也行，记下 THREAD_ID，回头 `--resume <THREAD_ID> --answer "确认"` 从上次停的地方接着跑。

## 装起来

需要 Python 3.11 以上。

```bash
pip install -r requirements.txt
cp .env.example .env        # 填一个模型密钥，默认读 DEEPSEEK_API_KEY
```

测试不用密钥，全跑本地的假模型，不花钱。跑测试要多装一个 pytest —— 产品运行用不到它，所以单独放一份：

```bash
pip install -r requirements-dev.txt
PYTHONPATH=src python -m pytest tests/ -q        # 819 passed
```

模型默认走 DeepSeek，Anthropic、Gemini、Ollama 也支持，在 `config.yaml` 的 `llm.providers` 里取消注释就能换。密钥只从环境变量读，不写进文件。

## 为什么有些活不放在流水线里

这条流水线是用 LangGraph 搭的：节点和连线都写在代码里，跑起来走固定的一条路。

好处是稳——同一份输入跑两遍，结论一样。代价是不灵活——跑到一半，它没法临时给自己长出一个新本事。

所以这个项目分了三层：每次都要跑、判定结果必须可复现的，放在流水线里；临时的判断和杂活，交给常驻的对话 Agent；会长、会变、别人已经做得更好的，放在项目外面。

放到外面的那些，用一张路由表管着（`references/外置工具路由.md`）。表上写清每样活：用哪个工具、装在哪个目录、怎么调、装了没有。

规矩有四条：

- 工具本体不进仓库（`.gitignore` 排掉了）。克隆下来，你拿到的是产品本体和这张表。
- 流水线在某个节点把数据递出去，工具干完把结果送回来。结论还是代码按数值算，工具不碰判断。
- 想加一样能力，改文件 + 在表上填一行就行，不用动流水线代码。
- 工具没装，Agent 会直说，给你两个选择：你自己提供，或者授权它去下那张表里已经筛过的那几个。

装的时候照这张表找位置：

| 用来做什么 | 装什么 | 装在哪 |
|---|---|---|
| 跑考题、给候选模型打分 | Promptfoo | `工具与参考/Agent外置工具包/AI评测工具包/` |
| 画流程图、架构图 | mermaid-cli | `工具与参考/Agent外置工具包/演示工具包/` |
| 做 PPT、网页演示 | 演示工具包 | 同上 |
| 查 AI 概念与论文 | ChromaDB + 嵌入模型 | 本地 `domain_kb/` |

领域知识库的语料得你自己准备。

## 仓库结构

```
src/            流水线本体：节点、判定、状态机、提示词、校验
tests/          819 个测试
scripts/        入口脚本（run_prd_workflow.py 等）
artifacts/      产物模板
.pi/skills/     常驻对话 Agent 的 PM 技能与操作手册
references/     项目自己写的参考：工具路由表、模型候选清单、流水线拓扑
docs/           核心规格：PRD、技术设计、工作流设计、职能标准调研、选型对位报告
AGENTS.md       给运行本产品的 Agent 读的人设与纪律
config.yaml     模型、知识库、阈值等配置
requirements.txt
requirements-dev.txt
.env.example
LICENSE
```

仓库里只有产品本体。会话记录、任务书、调研草稿这些开发过程文档留在本地，不上仓库。

## 还没做的

- 上线后的灰度监控和数据回流考题集只有接口，没实现
- 演示类产物（PPT、图表、原型）依赖外面的工具；工具没装，这一段只能给出「要做什么」的说明，出不了成品
- 领域知识库要自己建库；没有语料时，那一段的检索会直接跳过
- 人工确认点的周边设施（通知、超时、升级、审计）还没做全

## License

[MIT](LICENSE)
