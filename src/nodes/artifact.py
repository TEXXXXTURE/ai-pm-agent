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

        # 3. PRD 评审报告：red_team_review 非空时渲染 review.md.j2 落 .md；
        #    为空（理论上评审门必有输出）容错跳过，不阻断 PRD/洞察落盘。 [C 2026-09-10]
        artifacts_dict = {
            "prd": str(prd_path),
            "insights": str(insights_path),
        }
        review = state.get("red_team_review") or {}
        if review:
            review_md = deps.artifacts.render(
                "review.md.j2",
                {
                    "requirement_name": name,
                    "generated_at": generated_at,
                    "review": review,
                },
            ).strip()
            review_path = deps.artifacts.save(review_md, name, "review", ext=".md")
            artifacts_dict["review"] = str(review_path)

        # 4. 研发工单清单：issue_plan 非空时渲染 issues.md.j2 落 .md；
        #    为空（未经拆单节点）容错跳过，不阻断前三份产物。 [C 2026-09-11]
        issue_plan = state.get("issue_plan") or {}
        if issue_plan:
            issues_md = deps.artifacts.render(
                "issues.md.j2",
                {
                    "requirement_name": name,
                    "generated_at": generated_at,
                    "plan": issue_plan,
                },
            ).strip()
            issues_path = deps.artifacts.save(issues_md, name, "issues", ext=".md")
            artifacts_dict["issues"] = str(issues_path)

        return {
            "prd_markdown": md,
            "artifacts": artifacts_dict,
        }
        # [C 2026-09-09] T1 产物落 .md：prd_markdown 原文 + insights.md.j2 渲染
        # [C 2026-09-10] 新增评审报告 review.md.j2 渲染落盘 + artifacts["review"]
        # [C 2026-09-11] 新增工单清单 issues.md.j2 渲染落盘 + artifacts["issues"]

    return artifact_persist


# [C 2026-09-09] nodes/artifact.py 实现完成
# [C 2026-09-09] T1 改落 Markdown：PRD 原文 .md / 洞察 insights.md.j2 .md
