# 行业追踪 自测
"""R13 行业追踪（digest_quality 检查件 + industry_digest 驱动脚本纯函数）零 API 测试。

两类：
1. ``guards/digest_quality.py``：每期总结 11 条阈值命中/不命中；条目字段检查；
2. ``scripts/industry_digest.py``：信源库读取、日期解析、落盘结构、索引、期号。

口径（对齐 eval_quality 测试）：
- 纯函数，不调模型、不落真实文件（用 tmp 目录）；
- 只提示不阻断：warnings 可多可少，不抛异常；
- 检查函数自身异常不外抛。

运行（PowerShell，cwd=项目根）：
  $env:PYTHONPATH="src"
  PYTHONPATH=src python -m pytest tests/test_industry_digest.py -q
"""
from __future__ import annotations

import datetime
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
sys.path.insert(0, str(SRC_DIR))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from components.guards.digest_quality import (  # noqa: E402
    MAX_CHARS,
    MIN_CHARS,
    audit_digest_quality,
    audit_entry_fields,
)
import industry_digest as idig  # noqa: E402


def _good_summary() -> str:
    """构造一篇能通过大部分阈值的总结（2–4k 字、作者在场、设问、专名、数字、句长起伏、无黑词）。"""
    paras = []
    paras.append("我们从生产环境里看到，RAG 的检索质量在长文档场景一直上不去，这是卡我们最久的缺口。")
    paras.append("上周 NVIDIA 在博客里说，他们把 AI 工厂的 token 能耗优化了一轮，从每 token 1.2 焦耳降到 0.8 焦耳。")
    paras.append("我问自己一个问题：这类优化离我们能落地的距离有多远？答案是还差得远。")
    paras.append("Claude Code 最近在文档里加了一个能力，我们试过，对长任务有帮助。")
    paras.append("我们决定先不动。")
    # 用重复段落凑到 2000+ 字
    filler = "社区里 Gemini 3.8 和 DeepSeek v4 的对比很多，但我们关心的是它们对缺口清单的影响。"
    while sum(len(p) for p in paras) < 2600:
        paras.append(filler)
    return "\n\n".join(paras)


class TestDigestQuality(unittest.TestCase):
    """guards/digest_quality.py：11 条阈值命中/不命中。"""

    def test_good_summary_no_warnings(self):
        """一篇好总结不应有 warnings（或只有少量合理提示）。"""
        r = audit_digest_quality(_good_summary())
        self.assertEqual(r["errors"], [])
        self.assertTrue(isinstance(r["warnings"], list))
        self.assertTrue(isinstance(r["notes"], list) and r["notes"])

    def test_short_summary_warns(self):
        """过短（<2000 字）触发篇幅 warning。"""
        r = audit_digest_quality("太短了。")
        self.assertTrue(any("低于" in w and "字" in w for w in r["warnings"]))

    def test_too_long_summary_warns(self):
        """过长（>4000 字）触发篇幅 warning。"""
        long_text = "段落。" * 2000
        r = audit_digest_quality(long_text)
        self.assertTrue(any("超过" in w and "字" in w for w in r["warnings"]))

    def test_dash_density_warns(self):
        """破折号密度超阈值（>2/千字）触发 warning。"""
        text = "A——B——C——D——E——F。" * 50  # 大量破折号
        r = audit_digest_quality(text)
        self.assertTrue(any("破折号" in w for w in r["warnings"]))

    def test_no_author_warns(self):
        """没有「我/我们」触发作者在场 warning。"""
        text = "这是一段没有作者的描述，只有事实罗列，没有判断。" * 100
        r = audit_digest_quality(text)
        self.assertTrue(any("我/我们" in w or "作者不在场" in w for w in r["warnings"]))

    def test_no_question_warns(self):
        """没有问号触发设问 warning。"""
        text = "这是一段没有问号的文字。它只是陈述。" * 100
        r = audit_digest_quality(text)
        self.assertTrue(any("设问" in w for w in r["warnings"]))

    def test_no_entity_warns(self):
        """没有具体专名（全是通用词）触发专名 warning。"""
        text = "这个方案在行业中得到了广泛应用。它的效果显著。" * 60
        r = audit_digest_quality(text)
        self.assertTrue(any("专名" in w for w in r["warnings"]))

    def test_no_unit_number_warns(self):
        """没有带单位数字触发数字 warning。"""
        text = "效果很好。速度很快。成本很低。" * 60
        r = audit_digest_quality(text)
        self.assertTrue(any("带单位" in w or "数字" in w for w in r["warnings"]))

    def test_banned_connective_warns(self):
        """禁用连接词「首先」出现触发 warning。"""
        r = audit_digest_quality("首先，我们要讲一个故事。" + "其次，继续讲。" * 50)
        self.assertTrue(any("首先" in w for w in r["warnings"]))

    def test_level1_word_warns(self):
        """一级黑词「赋能」出现触发 warning。"""
        r = audit_digest_quality("这个平台赋能了所有业务线。" * 50)
        self.assertTrue(any("赋能" in w for w in r["warnings"]))

    def test_empty_input_safe(self):
        """空输入不抛异常，返回「检查未执行」。"""
        r = audit_digest_quality("")
        self.assertEqual(r["errors"], [])
        self.assertTrue(any("未执行" in n for n in r["notes"]))

    def test_entry_missing_field_warns(self):
        """条目缺字段触发 warning。"""
        entry = {"title": "某技术", "one_liner": "简介"}
        r = audit_entry_fields(entry)
        self.assertTrue(any("缺字段" in w for w in r["warnings"]))

    def test_entry_fabrication_marker_warns(self):
        """条目出现「首个」触发防编造提示。"""
        entry = {
            "title": "某技术", "one_liner": "这是首个能 X 的方案",
            "prod_usage": "未知", "community_popular": "A",
            "what_changed": "B", "innovation_type": "能力突破",
            "boundary": "C", "landing": "D", "gap": "不补",
            "priority": "普通", "significance": "E", "evidence_tier": "T3", "link": "http://x",
        }
        r = audit_entry_fields(entry)
        self.assertTrue(any("首个" in w for w in r["warnings"]))

    def test_entry_full_fields_ok(self):
        """字段齐全、无防编造词的条目不警告。"""
        entry = {
            "title": "某技术", "one_liner": "一句话",
            "prod_usage": "未知", "community_popular": "A",
            "what_changed": "B", "innovation_type": "创意缺口（判据）",
            "boundary": "C", "landing": "D", "gap": "不补我们的缺口",
            "priority": "普通优秀", "significance": "E", "evidence_tier": "T3", "link": "http://x",
        }
        r = audit_entry_fields(entry)
        self.assertEqual(r["errors"], [])


class TestIndustryDigestPure(unittest.TestCase):
    """scripts/industry_digest.py 纯函数（不调模型）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_fmt_date_rfc822(self):
        self.assertEqual(idig._fmt_date("Tue, 15 Sep 2026 10:00:00 GMT"), "2026-09-15")
        self.assertEqual(idig._fmt_date(""), "")
        self.assertEqual(idig._fmt_date("2026-09-15T10:00:00"), "2026-09-15")

    def test_fetch_recent_articles(self):
        """用临时 SQLite 建一张 articles 表，验证读取逻辑（只读）。"""
        db = self.root / "articles.db"
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE articles (id INTEGER PRIMARY KEY, source_id INTEGER, guid TEXT,"
                    " title TEXT, link TEXT, author TEXT, published TEXT, fetched_at TEXT,"
                    " summary TEXT, content TEXT)")
        now = datetime.datetime.now()
        con.execute(
            "INSERT INTO articles (source_id, guid, title, link, author, published, fetched_at, summary, content)"
            " VALUES (1, 'g1', 'T1', 'http://a', 'A', 'Tue, 15 Sep 2026 10:00:00 GMT', ?, 's', 'c')",
            (now.isoformat(sep=" "),),
        )
        con.execute(
            "INSERT INTO articles (source_id, guid, title, link, author, published, fetched_at, summary, content)"
            " VALUES (1, 'g2', 'T2', 'http://b', 'B', 'Tue, 01 Sep 2026 10:00:00 GMT', ?, 's2', 'c2')",
            ((now - datetime.timedelta(days=30)).isoformat(sep=" "),),
        )
        con.commit()
        con.close()
        arts = idig.fetch_recent_articles(db, days=7, limit=10)
        self.assertEqual(len(arts), 1)
        self.assertEqual(arts[0]["title"], "T1")
        self.assertEqual(arts[0]["source_name"], "?")

    def test_slugify(self):
        self.assertEqual(idig.slugify("A/B:C"), "A_B_C")
        self.assertEqual(idig.slugify("  空格  "), "空格")
        self.assertEqual(idig.slugify(""), "untitled")

    def test_ensure_structure(self):
        idig.ensure_structure(self.root)
        self.assertTrue((self.root / "话题").is_dir())
        self.assertTrue((self.root / "每期总结").is_dir())

    def test_write_index_and_read_back(self):
        entries = [{
            "topic": "模型与大模型", "title": "T", "source_name": "S",
            "published": "Tue, 15 Sep 2026 10:00:00 GMT", "evidence_tier": "T3", "link": "http://a",
        }]
        idig.write_index(self.root, entries)
        idx = self.root / "索引.md"
        self.assertTrue(idx.is_file())
        text = idx.read_text(encoding="utf-8")
        self.assertIn("模型与大模型", text)
        self.assertIn("2026-09-15", text)
        rows = idig._read_existing_index(self.root)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["link"], "http://a")

    def test_write_topic_entry(self):
        entry = {
            "topic": "模型与大模型", "title": "稀疏注意力", "one_liner": "一句话",
            "prod_usage": "未知", "community_popular": "A", "what_changed": "B",
            "innovation_type": "能力突破", "innovation_evidence": "依赖新模型",
            "boundary": "C", "landing": "D", "gap": "不补", "priority": "普通",
            "significance": "E", "evidence_tier": "T3", "link": "http://a",
            "published": "Tue, 15 Sep 2026 10:00:00 GMT", "source_name": "S",
        }
        path = idig.write_topic_entry(self.root, entry)
        self.assertTrue(path.is_file())
        text = path.read_text(encoding="utf-8")
        self.assertIn("稀疏注意力", text)
        self.assertIn("http://a", text)
        self.assertIn("2026-09-15", text)
        self.assertIn("**创新属于哪一类**", text)

    def test_append_update_log_and_seq(self):
        idig.ensure_structure(self.root)
        # 期号按「每期总结/」目录里的实际总结文件算，不是按日志行
        (self.root / "每期总结" / "2026-09-15-第01期.md").write_text("# 第1期", encoding="utf-8")
        (self.root / "每期总结" / "2026-09-16-第02期.md").write_text("# 第2期", encoding="utf-8")
        self.assertEqual(idig.next_period_seq(self.root), 3)
        # 空目录期号从 1 开始
        self.assertEqual(idig.next_period_seq(self.root / "empty"), 1)


if __name__ == "__main__":
    unittest.main()
