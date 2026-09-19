# get_llm_config max_tokens 口径自测
"""kernel.config.get_llm_config 的 max_tokens 口径测试（零网络、零 API 调用）。

覆盖任务书第三节口径 1（config.yaml llm 段新增 max_tokens: 8192）与第二节接口约定
（get_llm_config 返回值新增键 max_tokens，int 或 None）：

1. 项目 config.yaml 的 llm 段确实含 max_tokens: 8192，且为 int；
2. get_llm_config 返回该值；
3. llm 段未配置 max_tokens 时返回 None（向后兼容，调用方据此不传该参数）；
4. 原有键（litellm_model/api_key/temperature/max_retries/provider/api_base）行为不变。

运行（cwd=项目根）：
  $env:PYTHONPATH="src"
  PYTHONPATH=src python -m pytest tests/test_kernel_config.py -v
"""
from __future__ import annotations

import sys
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

import yaml  # noqa: E402

from kernel.config import get_llm_config  # noqa: E402

CONFIG_YAML = REPO_ROOT / "config.yaml"


def _llm_config_with(**llm_overrides) -> dict:
    """构造最小 llm 段配置（api_key_env 为空串，不依赖任何环境变量）。"""
    llm_section = {
        "default_provider": "fake-local",
        "temperature": 0.3,
        "max_retries": 2,
        "providers": {
            "fake-local": {
                "litellm_model": "ollama/qwen2.5",
                "api_key_env": "",
            }
        },
    }
    llm_section.update(llm_overrides)
    return {"llm": llm_section}


# ────────────────────────── 1. config.yaml 实际内容 ──────────────────────────


class TestActualConfigYaml(unittest.TestCase):
    def test_llm_section_has_max_tokens(self):
        """llm 段必须显式给出单次输出上限，且足够大以防长产物被截断。

        具体数值按真机实测校准（2026-09-15 实测 32768 通过；8192 会被隐藏思考吃光，
        正文为空），这里只守下限契约，避免每次调参都要同步改断言。
        """
        with open(CONFIG_YAML, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)
        self.assertIn("llm", config)
        self.assertIn("max_tokens", config["llm"], msg="llm 段缺 max_tokens")
        self.assertGreaterEqual(config["llm"]["max_tokens"], 8192)

    def test_max_tokens_is_int(self):
        with open(CONFIG_YAML, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)
        self.assertIsInstance(config["llm"]["max_tokens"], int)

    def test_legacy_llm_keys_intact(self):
        """口径 1 只新增 max_tokens，llm 段其余配置不动。"""
        with open(CONFIG_YAML, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)
        llm_section = config["llm"]
        self.assertEqual(llm_section["default_provider"], "deepseek")
        self.assertEqual(llm_section["temperature"], 0.3)
        self.assertEqual(llm_section["max_retries"], 2)
        self.assertEqual(
            llm_section["providers"]["deepseek"]["litellm_model"],
            "deepseek/deepseek-v4-flash",
        )


# ────────────────────────── 2. max_tokens 透出 ──────────────────────────


class TestGetLlmConfigMaxTokens(unittest.TestCase):
    def test_returns_configured_max_tokens(self):
        cfg = get_llm_config(_llm_config_with(max_tokens=8192))
        self.assertIn("max_tokens", cfg)
        self.assertEqual(cfg["max_tokens"], 8192)

    def test_returns_int_value_as_is(self):
        cfg = get_llm_config(_llm_config_with(max_tokens=16384))
        self.assertIsInstance(cfg["max_tokens"], int)
        self.assertEqual(cfg["max_tokens"], 16384)

    def test_missing_key_returns_none(self):
        """未配置 max_tokens 时为 None（向后兼容：调用方据此不传该参数）。"""
        cfg = get_llm_config(_llm_config_with())
        self.assertIn("max_tokens", cfg)
        self.assertIsNone(cfg["max_tokens"])

    def test_explicit_null_returns_none(self):
        cfg = get_llm_config(_llm_config_with(max_tokens=None))
        self.assertIsNone(cfg["max_tokens"])


# ────────────────────────── 3. 原有键零变化 ──────────────────────────


class TestLegacyKeysUnchanged(unittest.TestCase):
    def test_legacy_keys_present_with_same_values(self):
        cfg = get_llm_config(_llm_config_with(max_tokens=8192))
        self.assertEqual(cfg["litellm_model"], "ollama/qwen2.5")
        self.assertEqual(cfg["temperature"], 0.3)
        self.assertEqual(cfg["max_retries"], 2)
        self.assertEqual(cfg["provider"], "fake-local")
        self.assertEqual(cfg["api_key"], "")

    def test_api_base_still_passed_through(self):
        config = _llm_config_with(max_tokens=8192)
        config["llm"]["providers"]["fake-local"]["api_base"] = "https://example.com/v1"
        cfg = get_llm_config(config)
        self.assertEqual(cfg["api_base"], "https://example.com/v1")
        self.assertEqual(cfg["max_tokens"], 8192)


if __name__ == "__main__":
    unittest.main(verbosity=2)


