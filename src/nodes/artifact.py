# [C 2026-09-09] M6 纵切联调 - 产物持久化节点（artifact_persist，确定性）
# [C 2026-09-09] T1 产物改 Markdown 原生：
#   - PRD 不再渲染 HTML 模板，模型生成的 Markdown 全文（state["prd_markdown"]）strip 后直接落 .md；
#   - 需求洞察改用 insights.md.j2 渲染 Markdown 后落 .md；
#   - prd.html.j2 / insights.html.j2 + assets 退役为演示导出器，本节点不再调用。
"""artifact_persist：把 PRD Markdown 全文与需求洞察 Markdown 按需求名分文件夹落盘（.md）。"""
from __future__ import annotations

from datetime import datetime

from kernel.exceptions import NodeExecutionError


def make_artifact_persist(deps):
    """产物持久化（不调模型）。"""

    def artifact_persist(state: dict) -> dict:
        name = state.get("requirement_name") or "未命名需求"
        generated_at = datetime.now().strftime("%Y-%m-%d")

        # 1. PRD：模型原生 Markdown 全文直接落盘（T1）；为空说明上游异常，快速失败
        md = (state.get("prd_markdown") or "").strip()
        if not md:
            raise NodeExecutionError("artifact_persist", "prd_markdown 为空，无法落盘")
        prd_path = deps.artifacts.save(md, name, "prd", ext=".md")

        # 2. 需求洞察：Markdown 版 Jinja2 模板容错渲染后落盘（T1）
        insights_md = deps.artifacts.render(
            "insights.md.j2",
            {
                "requirement_name": name,
                "generated_at": generated_at,
                "user_insights": state.get("user_insights", {}),
            },
        ).strip()
        insights_path = deps.artifacts.save(insights_md, name, "insights", ext=".md")

        return {
            "prd_markdown": md,
            "artifacts": {"prd": str(prd_path), "insights": str(insights_path)},
        }
        # [C 2026-09-09] T1 产物落 .md：prd_markdown 原文 + insights.md.j2 渲染

    return artifact_persist


# [C 2026-09-09] nodes/artifact.py 实现完成
# [C 2026-09-09] T1 改落 Markdown：PRD 原文 .md / 洞察 insights.md.j2 .md
