---
name: eval-tool-routing
description: AI 测评工具路由技能。当用户问「这个功能要怎么测」「该用 promptfoo 还是 DeepEval/Ragas/lm-evaluation-harness」「想给模型跑公开基准」「想测 Agent 的工具调用和错误恢复」「想测输出内容合不合格」「想做上线前回归、上线后漂移重跑、红队对抗」等 AI 测评问题时使用。技能不包含测评逻辑，只负责判断测的是七层中的哪一层、该用哪个工具、怎么调、结果在哪；同一调用路径重复验证两次以上，按 references 末尾规则固化，不再重复路由。软件工程的单元/集成测试不在本技能范围。
---

# AI 测评工具路由（eval-tool-routing）

「AI 测试」不是一个动作，是两个阵营、七层对象的混称。先判断测的是哪一层，再选工具。不要凭「测试」二字直接选 promptfoo。

## 一、先定对象：七层速判

| 层 | 测什么 | 一句话判据 |
|---|---|---|
| L1 模型能力 | 裸模型在公开题集的得分 | 问题是「这个模型行不行」，与我们的产品无关 |
| L2 评测方法与基础设施 | 基准分是否可信：污染、饱和、指标口径 | 问题是「这个分数能不能信」 |
| L3 系统工程 | 围绕模型的软件：组件、集成、E2E、成本延迟 | 问题是「这套系统跑不跑得通」 |
| L4 Agent 与流程 | 多步过程：任务成功率、工具选择、参数、错误恢复、轨迹 | 问题是「过程做得对不对」，不是结果好不好 |
| L5 内容输出 | 格式、事实性、幻觉、语气、术语、指令遵循 | 问题是「说出来的话合不合格」 |
| L6 在线生产 | 影子、灰度、A/B、漂移、用户反馈、人机回退率 | 问题是「上线后有没有变坏」 |
| L7 组织流程 | eval 驱动开发、红队、golden set、版本管理 | 问题是「怎么把前面六层持续跑起来」 |

L1–L2 是模型评测阵营；L3–L6 是业务评测阵营；L7 是机制。软件工程的单元/组件测试不属本技能，走普通研发测试。

## 二、路由总表（每层用什么）

| 需求 | 工具 | 形态与调用 | 本机现状 |
|---|---|---|---|
| L1 跑公开基准（HLE、MMLU-Pro、AIME 等） | **lm-evaluation-harness** | Python CLI：`lm_eval --model hf --model_args pretrained=X --tasks y --output_path z` | 未安装 |
| L1 一次编排多个基准/出对比榜 | OpenCompass 或 HELM | Python 配置驱动 | 未安装 |
| L2 污染检查 | Giskard 扫描 / 各 harness 自带 contamination 报告 | Python | 未安装 |
| L3 组件/集成的语义断言 | **DeepEval**（挂 pytest） | Python：指标类作断言 | 未安装 |
| L4 Agent 多步过程评测 | DeepEval 的 agent/goal 指标 或 **Inspect**（solver+scorer 组合） | Python | 未安装 |
| L4 RAG 组件指标 | **Ragas**（忠实度、上下文精确率/召回率） | Python：Sample 对象 | 未安装 |
| L5 内容输出评测（主力） | **promptfoo** | YAML 配置 + `promptfoo eval -c x.yaml`，结果出 JSON 和网页 | **已安装 v0.123** |
| L5 无标准答案的语义判分 | promptfoo 模型裁判（rubric）或 DeepEval G-Eval | 同上 / Python | promptfoo 可用 |
| L6 上线后漂移重跑 | promptfoo 同配置同回归考题定期重跑 | CLI + 定时任务 | 可用 |
| L6 线上轨迹与上报（需服务端时） | Langfuse 或 Arize Phoenix | 独立服务 + SDK | 未安装，且不建议为本项目建 |
| L7 红队对抗题 | **promptfoo redteam**（13 个行业方向） | `promptfoo redteam run` | 可用 |
| L7 golden set / 评测流程 | Inspect 的 dataset/log 体系，或直接用 promptfoo 配置管理 | — | 未安装 |

## 三、主力工具怎么调（promptfoo，本机已装）

最小 YAML 结构：`prompts`（被测系统提示词文件）、`providers`（被测模型与参数）、`tests`（每题 `vars` + `assert`）。

- 断言类型：`equals / contains / icontains / regex / is-json / javascript / latency / cost`；语义类用 `llm-rubric`、`factuality`、`model-graded-closedqa`。
- 执行：`npx promptfoo eval -c 配置.yaml`；看网页 `npx promptfoo view`；机器读结果在输出目录 `results.json`。
- 红队：`npx promptfoo redteam init` 生成配置，`npx promptfoo redteam run` 生成并执行对抗题。
- 费用来自调用被测模型 API；代理会掐断长响应，调用 DeepSeek 前清空代理环境变量。

## 四、其余工具的安装与选择口径

- **要挂 CI、写 pytest 断言** → DeepEval（Apache-2.0）：`pip install deepeval`，指标 40+，含 agent、忠实度、幻觉、PII。
- **测 RAG** → Ragas（Apache-2.0）：`pip install ragas`，忠实度用「先拆原子陈述、再逐句对照上下文」流程。
- **要测复杂多步过程、结构要干净** → Inspect（MIT，英国 AISI）：`pip install inspect_ai`，五件 `eval/solver/scorer/dataset/tool`。
- **跑模型公开基准** → lm-evaluation-harness（MIT）：`pip install lm-eval`。
- 同一需求多个工具可做：跟代码工程走（项目已用 pytest 选 DeepEval，已有 YAML 选 promptfoo），不为单一工具引入新语言。

## 五、回答与执行规则

1. 先向用户说清：测的是七层哪一层、用哪个工具、为什么；不直接抛工具名。
2. 工具未安装时如实说明，给安装方式，不假装调用成功。
3. 模型裁判、漂移结论等涉及判断的，阈值和判据由人拍板，不替用户放行。
4. 判断一个测评能不能测出想要的数据（题库来历、筛题规则、污染饱和、判分局限）→ 读 `references/测评内容调研.md`，这是选型的主依据；各工具的形态、许可证、复刻范围读 `references/测评工具拆解分析.md`。

## 六、路由固化规则

每次实际调用后，在 `references/调用登记.md` 追加一行：日期、层、需求、走的工具、结果能否直接用、别扭点。同一需求同一路径走过两次以上且验证可用，把它提升为本文件的固定条目（或我们工具的默认功能），此后不再重复选择；现有工具确认满足不了的方向（L4 多步、L6 漂移重跑为高概率项），按拆解文档的复刻路线启动自建模块。
