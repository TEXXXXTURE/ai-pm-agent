# [C 2026-09-09] M4 产物系统 - HTML 产物渲染与按需求名分文件夹落盘
# [C 2026-09-09] T1 产物改 Markdown 原生 - save() 增加 ext 参数支持 .md 落盘；
#     render() 与 HTML 相关逻辑原样保留（prd.html.j2 / insights.html.j2 + assets 退役为演示导出器）。
"""ArtifactManager：Jinja2 模板渲染 + 按需求名分文件夹落盘（T1 起主产物为 Markdown）。

- 模板位于 artifacts/templates/，静态资源（style.css / editor.js）位于 artifacts/assets/
- render() 渲染模板：HTML 模板渲染时将 css/js 全文内联（自包含、可编辑/打印/复制）；
  T1 新增 Markdown 模板（insights.md.j2），不依赖 assets
- save() 落盘结构：<output_root>/<需求名>/<中文子目录>/<需求名>-<doc_type><ext>
  ext 默认 ".html"；T1 起 PRD/洞察主产物传 ext=".md"
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Union

from jinja2 import Environment, FileSystemLoader

# doc_type 英文标识 → 中文子目录名（未知 doc_type 直接用其本身作为目录名）
_DOC_DIR_MAP = {
    "prd": "需求文档",
    "insights": "需求洞察",
    # [C 2026-09-11] 研发工单清单落「研发工单」子目录；review 目录维持英文原样不动
    "issues": "研发工单",
    # [C 2026-09-11] 发布计划落「发布计划」子目录
    "launch_plan": "发布计划",
}

# 文件系统非法字符（Windows 全量，跨平台保守处理）
_ILLEGAL_CHARS = re.compile(r'[\\/:*?"<>|]')


def sanitize_name(name: str) -> str:
    """将需求名中的文件/目录非法字符替换为下划线。"""
    return _ILLEGAL_CHARS.sub("_", str(name)).strip()


class ArtifactManager:
    """渲染模板并保存产物（T1 起主产物为 Markdown .md；HTML 通道保留为演示导出器）。"""

    def __init__(
        self,
        output_root: str = "./output",
        template_dir: str = "./artifacts/templates",
        assets_dir: str = "./artifacts/assets",
    ) -> None:
        self.output_root = Path(output_root)
        self.template_dir = Path(template_dir)
        self.assets_dir = Path(assets_dir)
        # autoescape=False：style_css / editor_js / body_html 均需原样注入
        self._env = Environment(
            loader=FileSystemLoader(str(self.template_dir)),
            autoescape=False,
            keep_trailing_newline=True,
        )

    def _load_asset(self, name: str) -> str:
        """读取 assets 目录下静态资源（css/js）的全文内容。"""
        return (self.assets_dir / name).read_text(encoding="utf-8")

    def render(self, template_name: str, context: dict) -> str:
        """渲染 Jinja2 模板，自动注入 style_css / editor_js 全文。

        Args:
            template_name: 模板文件名，如 "prd.html.j2"
            context: 模板上下文（requirement_name / generated_at / sections 等）

        Returns:
            渲染后的完整 HTML 字符串（自包含，可直接落盘）
        """
        ctx = dict(context)
        ctx.setdefault("style_css", self._load_asset("style.css"))
        ctx.setdefault("editor_js", self._load_asset("editor.js"))
        template = self._env.get_template(template_name)
        return template.render(**ctx)

    def save(
        self,
        content: str,
        requirement_name: str,
        doc_type: str,
        ext: str = ".html",
    ) -> Path:
        """按 需求名/中文子目录/需求名-doc_type<ext> 结构落盘。

        例：output/ai-cs/需求文档/ai-cs-prd.md   （T1：Markdown 主产物，ext=".md"）
            output/ai-cs/需求洞察/ai-cs-insights.md
            output/ai-cs/需求文档/ai-cs-prd.html （HTML 演示导出器，ext 默认 ".html"）

        Args:
            content: 待写入的文件全文（Markdown 或 HTML）
            requirement_name: 需求名（用作文件夹与文件名，非法字符会被替换）
            doc_type: 产物类型标识（prd / insights / 其他）
            ext: 文件扩展名（含点），默认 ".html"；Markdown 产物传 ".md"

        Returns:
            写入文件的 Path
        """
        safe_name = sanitize_name(requirement_name)
        sub_dir = _DOC_DIR_MAP.get(doc_type, doc_type)
        out_dir = self.output_root / safe_name / sub_dir
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{safe_name}-{doc_type}{ext}"
        out_path.write_text(content, encoding="utf-8")
        return out_path
        # [C 2026-09-09] T1 save() 增加 ext 参数，文件名改为 {name}-{doc_type}{ext}

    def load(self, path: Union[str, Path]) -> str:
        """读回已保存的产物文件内容。"""
        return Path(path).read_text(encoding="utf-8")


# [C 2026-09-09] artifact.py 实现完成
# [C 2026-09-09] T1 save() 支持 .md 落盘；render()/HTML 逻辑原样保留
