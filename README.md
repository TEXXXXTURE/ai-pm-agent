# AI PM Agent

![tests](https://github.com/TEXXXXTURE/ai-pm-agent/actions/workflows/tests.yml/badge.svg)

![Python](https://img.shields.io/badge/Python-3.11%2B-blue)

![License](https://img.shields.io/badge/License-MIT-green)

产品需求执行流水线。输入一句需求，按固定顺序走完 12 段节点，中间 8 处暂停等待人工确认，产出 PRD、评审结论、研发工单、发布计划。

范围：需求判定、PRD、评审、评测体系、模型选型、研发工单、发布计划。判定覆盖四类：需求是否 AI 核心、模型能否达成、完成标准定义、质量与成本责任归属。

## 流程

需求先判定轨道：AI 核心需求走完整 12 段；普通需求跳过 AI 专项段。

| 段 | 动作 | 产物 |
|---|---|---|
| 0–0.5 | 接收需求、修订整合 | 需求草案（等待确认） |
| 1 | 挖需求、判断是否 AI 核心 | 需求理解记录、初始考题 |
| 2 | 判断需求与 AI 的边界 | 三方对照 + 探针实测证据 |
| 3 | 写 PRD | ai-native PRD |
| 4 | 评审 PRD | 五维评审报告，一票否决 |
| 5 | 设计评测体系 | 四层考题 + 双及格线 |
| 6 | 对比选型模型 | 候选各跑一遍，代码硬判推荐 |
| 7 | 拆研发工单 | 工单 + 覆盖矩阵 |
| 8 | 构建期跑评测 | 真跑结果，不达线不放行 |
| 9 | 写发布计划 | 7 步计划 + 灰度 / 回滚 / go-no-go |
| 收尾 | 产物写文件 | 全部落 output/ |

## 人工闸门（8 处）

需求确认 / 草案确认 / 可行性确认 / 评测体系确认 / 对比选型 / 工单确认 / 发布计划确认 / 评测执行。

闸门收到回复后继续；未收到回复时流水线保持暂停。中断时记录 THREAD_ID，以 `--resume <THREAD_ID> --answer "确认"` 续跑。

## 判定规则

1. **结论由代码判定**：模型只产草案（打分 / JSON / 清单）；评审过不过、评测达不达标、模型选择、就绪度全部由代码按数值计算。换模型不影响结论；同一输入两遍运行结果一致。
2. **模型能力靠实测**：第 2 段用真实样例跑探针，能力、边界、工具兜底以证据为准；证据不齐不放行。
3. **评测先于开发**：需求确认即建考题（典型 / 边界 / 对抗 / 回放四层），开发前定义完成标准；构建期真跑评测，不达及格线不放行。
4. **结论带证据等级**：关键结论标 [T1]–[T5] 来源等级（实测 / 用户 / 分析 / 口述 / 直觉），低等级驱动的决策显式标注；发布前做 11 维就绪度打分与阻断项检查。

## 架构

三层边界：

- 内核（`src/`）：判定与契约。路由由代码决定，模型不决定走向；结论一律代码硬判。
- SOP 层（`AGENTS.md`、`references/`、`docs/`）：规则与清单。
- 外置层（`scripts/`、`.pi/skills/`、外置工具包、`domain_kb/`）：可替换执行件与内容。

原则：执行可外置，判定留内核；内容可外置，契约留内核。

## 目录

```
src/            流水线本体：节点、判定、状态机、提示词、校验
tests/          测试（本地假模型，零 API 费）
scripts/        入口脚本
artifacts/      产物模板（assets/ 素材、templates/ 模板）
.pi/skills/     供本地 pi CLI Agent 加载的 PM 技能与操作手册
references/     工具路由表、模型候选清单、流水线拓扑
docs/           PRD、技术设计、工作流设计、职能标准调研、选型对位报告
学习文档/       评测方法论学习笔记（ai-evaluation）
工具与参考/     自制工具包与第三方参考资料归档
AGENTS.md       给运行本产品的 Agent 读的纪律
config.yaml     模型、知识库、阈值等配置
```

## 运行

```bash
PYTHONPATH=src python scripts/run_prd_workflow.py \
  --requirement "客服回复建议助手：客服与用户聊天时，AI 根据上下文生成 1-3 条回复建议供一键采用；识别到情绪激烈或涉及投诉赔付时不给建议，提示转人工组长。" \
  --name 客服回复建议助手
```

启动后先复述需求、判断是否 AI 核心需求，随后暂停等待确认：

```
STATUS: HITL
THREAD_ID: 4461143c-6ed5-4de8-9e51-981a3c45312e
NODE: requirement_confirm
QUESTION: 节点「requirement_confirm」进入需求确认门。请审阅需求理解与 AI 适用性分流建议，
确认无误后回复 confirmed，或回复「非AI」/「AI核心」改判轨道；其他文本作为需求修订意见。
```

产物落在 `output/<name>/`：

```
output/客服回复建议助手/
├── 需求文档/   客服回复建议助手-prd.md
├── 需求洞察/   客服回复建议助手-insights.md
├── review/     客服回复建议助手-review.md
├── 研发工单/   客服回复建议助手-issues.md
├── 发布计划/   客服回复建议助手-launch_plan.md
└── 评测/       eval_report.md、bakeoff_report.md、results.json
```

## 安装与配置

- 需要 Python 3.11+
- `pip install -r requirements.txt`；`cp .env.example .env`（填 DEEPSEEK_API_KEY）
- 测试免密钥、零 API 费：`pip install -r requirements-dev.txt`；`PYTHONPATH=src python -m pytest tests/ -q`
- 模型默认 DeepSeek；Anthropic、Gemini、Ollama 在 `config.yaml` 的 `llm.providers` 取消注释切换；密钥只从环境变量读取

## 不适用 / 未实现

- 代码编写、bug 修复、工程搭建——范围外
- 运行时动态新增能力——节点与连线写死，不运行时扩展
- 领域知识检索——语料自备；无语料时该段跳过
- 演示类产物（PPT、图表、原型）——依赖外置工具；未安装时仅输出说明，不生成成品
- 人工确认点周边设施（通知、超时、升级、审计）——未实现完整
- 上线后灰度监控与数据回流——接口已留，实现未做

## License

[MIT](LICENSE)
