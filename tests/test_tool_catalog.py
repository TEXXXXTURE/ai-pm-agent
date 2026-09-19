# [C 2026-09-14 by S043-b1] 工具能力清单加载器自测
"""tool_catalog 加载器零依赖测试：用项目实际路由表 + 临时文件覆盖四类场景。

覆盖：
1. load_tool_catalog 正常解析项目 references/外置工具路由.md：
   - 返回非空列表；
   - 每项含 name/can_do/cannot_do/prerequisite/category 五字段；
   - category 只能是 "产物能力" 或 "执行器"；
   - 已装 5 个工具（4 个产物能力 + 1 个执行器 Promptfoo）都被解析出。
2. 文件不存在时安全降级返回空列表（不抛错）。
3. 文件内容为空字符串时返回空列表。
4. 表格行中没有"能做："前缀时被跳过（用临时文件构造）。

运行（PowerShell，cwd=项目根）：
  $env:PYTHONPATH="src"
  PYTHONPATH=src python -m pytest tests/test_tool_catalog.py -v
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

# Windows GBK 控制台兜底
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
sys.path.insert(0, str(SRC_DIR))

import unittest  # noqa: E402

from components.tools.tool_catalog import load_tool_catalog  # noqa: E402

ROUTING_MD = REPO_ROOT / "references" / "外置工具路由.md"

_REQUIRED_KEYS = ("name", "can_do", "cannot_do", "prerequisite", "category")
_VALID_CATEGORIES = ("产物能力", "执行器")


# ────────────────────────── 1. 正常解析项目路由表 ──────────────────────────


class TestLoadToolCatalogActual(unittest.TestCase):
    def test_parses_actual_routing_md(self):
        catalog = load_tool_catalog(str(ROUTING_MD))
        self.assertTrue(catalog, "应解析出至少一个工具")

        for item in catalog:
            for key in _REQUIRED_KEYS:
                self.assertIn(key, item, msg=f"缺字段 {key}: {item!r}")
            self.assertIn(item["category"], _VALID_CATEGORIES)

    def test_installed_tools_all_parsed(self):
        """已装的 5 个工具（4 产物能力 + 1 执行器）都被解析出。"""
        catalog = load_tool_catalog(str(ROUTING_MD))
        names = {item["name"] for item in catalog}
        for tool_name in (
            "frontend-slides",
            "lieflat-charts",
            "ppt-master",
            "diagram-mermaid（mermaid-cli）",
            "Promptfoo 0.123.0",
        ):
            self.assertIn(tool_name, names, msg=f"缺工具: {tool_name}")

    def test_categories_correct(self):
        """第二节工具 category=产物能力，二·补节 Promptfoo category=执行器。"""
        catalog = load_tool_catalog(str(ROUTING_MD))
        product_tools = [
            t for t in catalog if t["category"] == "产物能力"
        ]
        executor_tools = [
            t for t in catalog if t["category"] == "执行器"
        ]
        self.assertEqual(len(product_tools), 4, f"产物能力应 4 个: {product_tools!r}")
        self.assertEqual(len(executor_tools), 1, f"执行器应 1 个: {executor_tools!r}")
        self.assertEqual(executor_tools[0]["name"], "Promptfoo 0.123.0")

    def test_three_elements_nonempty(self):
        """已装工具的三要素（能做/不能做/调用前提）均非空。"""
        catalog = load_tool_catalog(str(ROUTING_MD))
        for item in catalog:
            self.assertTrue(item["can_do"], msg=f"can_do 空: {item['name']!r}")
            self.assertTrue(item["cannot_do"], msg=f"cannot_do 空: {item['name']!r}")
            self.assertTrue(
                item["prerequisite"], msg=f"prerequisite 空: {item['name']!r}"
            )


# ────────────────────────── 2. 文件不存在 ──────────────────────────


class TestLoadToolCatalogMissingFile(unittest.TestCase):
    def test_missing_file_returns_empty(self):
        catalog = load_tool_catalog(
            "/nonexistent/path/to/routing_table_missing.md"
        )
        self.assertEqual(catalog, [])


# ────────────────────────── 3. 空文件 ──────────────────────────


class TestLoadToolCatalogEmptyFile(unittest.TestCase):
    def test_empty_file_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "empty.md"
            path.write_text("", encoding="utf-8")
            catalog = load_tool_catalog(str(path))
            self.assertEqual(catalog, [])

    def test_whitespace_only_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ws.md"
            path.write_text("   \n  \n", encoding="utf-8")
            catalog = load_tool_catalog(str(path))
            self.assertEqual(catalog, [])


# ────────────────────────── 4. 无"能做："前缀的行被跳过 ──────────────────────────


class TestLoadToolCatalogNoPrefix(unittest.TestCase):
    def test_rows_without_prefix_skipped(self):
        content = """# 测试表

## 二、工具目录

| 期望形式 | 工具/技能 | 能力说明 | 来源 | 安装状态 | 调用入口 |
|---|---|---|---|---|---|
| HTML 网页 | some-tool | 普通描述没有前缀 | https://example.com | ⬚ 备选 | 路径 |
| 数据图表 | another-tool | 也没有能做前缀 | https://example.com | ⬚ 备选 | 路径 |

## 三、外置包位置约定

无关内容。
"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "no_prefix.md"
            path.write_text(content, encoding="utf-8")
            catalog = load_tool_catalog(str(path))
            self.assertEqual(catalog, [])

    def test_mixed_rows_only_parses_prefixed(self):
        """混排表中只解析含"能做："前缀的行，无前缀的跳过。"""
        content = """# 测试表

## 二、工具目录

| 期望形式 | 工具/技能 | 能力说明 | 来源 | 安装状态 | 调用入口 |
|---|---|---|---|---|---|
| HTML 网页 | has-prefix | 能做：单文件 HTML；不能做：PPTX；调用前提：加载技能 | https://x | ✅ 已装 | 路径 |
| 数据图表 | no-prefix | 普通描述没有前缀 | https://y | ⬚ 备选 | 路径 |

## 三、外置包位置约定
"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mixed.md"
            path.write_text(content, encoding="utf-8")
            catalog = load_tool_catalog(str(path))
            self.assertEqual(len(catalog), 1)
            self.assertEqual(catalog[0]["name"], "has-prefix")
            self.assertEqual(catalog[0]["can_do"], "单文件 HTML")
            self.assertEqual(catalog[0]["cannot_do"], "PPTX")
            self.assertEqual(catalog[0]["prerequisite"], "加载技能")
            self.assertEqual(catalog[0]["category"], "产物能力")


if __name__ == "__main__":
    unittest.main(verbosity=2)


# [C 2026-09-14 by S043-b1] tests/test_tool_catalog.py 新增完成
