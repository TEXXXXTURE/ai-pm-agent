# [C 2026-09-09] M6 纵切联调 - PRD 阶段节点（prd_generation）
# [C 2026-09-09] M7.5 创意放松 - 去除机器 guard 自动打回，self_checks 用默认空列表（NodeRunner 自动跳过自检）；
#     质量引导下沉到 prompt 的"资深 PM 质量标杆"自检。guards/prd_checks.py 保留备用，本节点不再挂载。
# [C 2026-09-09] T1 产物改 Markdown 原生 - 不再 load_schema / 不再产出 JSON sections+HTML 片段，
#     改走 NodeRunner.run_text 文本通道：模型按 prd_generation.md 要求直接输出 Markdown 全文，
#     原文写入 state["prd_markdown"]，由 artifact_persist 节点落盘为 .md。
"""PRD 生成节点工厂：模型原生输出完整 PRD 的 Markdown 全文（T1 起）。

T1 之前：模型输出 JSON（sections + body_html 片段），再由 Jinja2 HTML 模板套外壳。
T1 起：NodeSpec 只挂 prompt（output_schema 默认 None），走 runner.run_text 文本通道——
不做 schema 校验、不做 guard 自检，模型原文即产物，落盘 .md 由 artifact_persist 负责。
"""
from __future__ import annotations

from kernel.spec import NodeSpec


def make_prd_generation(deps):
    """PRD 生成：模型原生输出 Markdown 全文，返回 prd_markdown 并清零回炉意见。

    [C 2026-09-12 by MA] S033 块2a：按 state["ai_core"] 选模板——
    - ai_core=True：读取 prd_generation_ai_native.md（AI-native 模板，八项必含）
    - ai_core=False / None / 缺失：读取 prd_generation.md（普通模板，逐字不变）
    普通轨行为零变化；模板选择只影响 prompt_template 字段。
    """

    def prd_generation(state: dict) -> dict:
        # [C 2026-09-12 by MA] S033 块2a：分流选模板
        ai_core = state.get("ai_core")
        prompt_name = (
            "prd_generation_ai_native" if ai_core is True else "prd_generation"
        )
        prompt = deps.registry.read_prompt(prompt_name)
        # [C 2026-09-09] T1 不传 output_schema（NodeSpec 默认 None）、不挂 self_checks；
        # 文本通道无需 schema 校验，创意质量由 prompt 的质量标杆自检引导
        spec = NodeSpec(
            name="prd_generation",
            prompt_template=prompt,
        )
        text = deps.runner.run_text(spec, state)
        # [C 2026-09-11] 块2：工单确认门"回PRD"意见只注入本轮重写一次，消费即清零，
        # 避免后续评审打回重跑时陈旧回炉意见被反复注入
        return {"prd_markdown": text, "prd_rewrite_feedback": ""}

    return prd_generation


# [C 2026-09-09] T1 nodes/prd.py 改走 run_text 文本通道，输出 prd_markdown
# [C 2026-09-12 by MA] S033 块2a：按 ai_core 选 ai-native / 普通 PRD 模板
