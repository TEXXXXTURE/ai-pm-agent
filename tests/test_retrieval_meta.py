"""检索元数据与阶段/版本硬过滤（Three.js/WebGPU 预设方案 2.2 / 2.3）。

覆盖：
- contract.parse_document 提取检索元数据（列表→CSV 串、weight→float、日期归一化、
  evidence 嵌套与扁平两种写法、空值不入库保持老文档形态）
- store._phase_ok / _version_ok / _hard_filter（无标记放行、命中/未命中、
  invalid_after 边界、版本号解析失败不误杀）
"""

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from kb.rag.contract import parse_document  # noqa: E402
from kb.rag.store import _hard_filter, _phase_ok, _version_ok  # noqa: E402


DOC = """---
title: TSL 节点类型陷阱
source: threejs-failures
type: snippet
updated: 2026-10-16
phase_tags: [材质, 后处理]
valid_for_versions: [r183, r184]
invalid_after: r185
constraint_type: negative
entity_type: failure
weight: 0.8
last_verified: 2026-10-16
why_not_the_obvious_way: 直接隐式转换在 r183 后语义变了
evidence:
  source: 官方迁移指南
  url: https://example.com/migration
  date: 2026-10-16
  level: official
---

正文。
"""


class TestParseRetrievalMeta(unittest.TestCase):
    def test_frontmatter_fields_normalized(self):
        result = parse_document(DOC)
        self.assertEqual(result["phase_tags"], "材质,后处理")
        self.assertEqual(result["valid_for_versions"], "r183,r184")
        self.assertEqual(result["invalid_after"], "r185")
        self.assertEqual(result["constraint_type"], "negative")
        self.assertEqual(result["entity_type"], "failure")
        self.assertEqual(result["weight"], 0.8)
        self.assertEqual(result["last_verified"], "2026-10-16")
        self.assertIn("隐式转换", result["why_not_the_obvious_way"])
        self.assertEqual(result["evidence_source"], "官方迁移指南")
        self.assertEqual(result["evidence_url"], "https://example.com/migration")
        self.assertEqual(result["evidence_date"], "2026-10-16")
        self.assertEqual(result["evidence_level"], "official")

    def test_no_fields_means_no_retrieval_keys(self):
        plain = "---\ntitle: t\nsource: s\ntype: note\nupdated: 2026-10-16\n---\nbody"
        result = parse_document(plain)
        for key in ("phase_tags", "valid_for_versions", "constraint_type", "weight"):
            self.assertNotIn(key, result, msg=f"无标记文档不应带 {key}: {result!r}")

    def test_bad_weight_dropped(self):
        doc = (
            "---\ntitle: t\nsource: s\ntype: note\nupdated: 2026-10-16\n"
            "weight: abc\n---\nbody"
        )
        result = parse_document(doc)
        self.assertNotIn("weight", result)

    def test_evidence_flat_keys_equivalent(self):
        doc = (
            "---\ntitle: t\nsource: s\ntype: note\nupdated: 2026-10-16\n"
            "evidence_source: x\nevidence_level: community\n---\nbody"
        )
        result = parse_document(doc)
        self.assertEqual(result["evidence_source"], "x")
        self.assertEqual(result["evidence_level"], "community")


class TestHardFilter(unittest.TestCase):
    @staticmethod
    def _row(**meta):
        return {"metadata": meta}

    def test_phase_ok(self):
        self.assertTrue(_phase_ok({}, "材质"))
        self.assertTrue(_phase_ok({"phase_tags": ""}, "材质"))
        self.assertTrue(_phase_ok({"phase_tags": "材质,后处理"}, "材质"))
        self.assertFalse(_phase_ok({"phase_tags": "基建"}, "材质"))
        self.assertTrue(_phase_ok({"phase_tags": "基建"}, None))

    def test_version_ok(self):
        self.assertTrue(_version_ok({}, "r184"))
        self.assertTrue(_version_ok({"valid_for_versions": "r183,r184"}, "r184"))
        self.assertFalse(_version_ok({"valid_for_versions": "r183,r184"}, "r185"))
        self.assertFalse(_version_ok({"invalid_after": "r185"}, "r185"))
        self.assertTrue(_version_ok({"invalid_after": "r185"}, "r184"))
        # 版本号解析失败不误杀
        self.assertTrue(_version_ok({"invalid_after": "nightly"}, "r184"))
        self.assertTrue(_version_ok({"valid_for_versions": "main"}, "r184"))

    def test_hard_filter_rows(self):
        rows = [
            self._row(name="a", phase_tags="材质", invalid_after="r185"),
            self._row(name="b", phase_tags="基建"),
            self._row(name="c"),
        ]
        kept = _hard_filter(rows, phase="材质", version="r184")
        self.assertEqual([r["metadata"]["name"] for r in kept], ["a", "c"])
        kept = _hard_filter(rows, phase=None, version=None)
        self.assertEqual(len(kept), 3)


if __name__ == "__main__":
    unittest.main()
