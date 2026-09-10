# [MA 2026-09-08] 配置加载器：读取 config.yaml + .env，提供 LLM Provider 配置
# [C 2026-09-09] get_llm_config 改为 LiteLLM 模型串格式（litellm_model），支持本地模型空密钥
"""全局配置加载模块。

用法:
    from kernel.config import load_config, get_llm_config
    config = load_config()
    llm_cfg = get_llm_config(config)  # 返回 {litellm_model, api_key, temperature, max_retries, provider, ...}
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv


# 项目根目录（ai-pm-agent/）
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


def load_config(config_path: str | Path | None = None) -> dict[str, Any]:
    """加载 config.yaml，并自动加载同目录下的 .env。"""
    if config_path is None:
        config_path = PROJECT_ROOT / "config.yaml"
    else:
        config_path = Path(config_path)

    # 加载 .env（与 config.yaml 同目录或项目根）
    env_path = config_path.parent / ".env"
    if env_path.exists():
        load_dotenv(env_path)
    elif (PROJECT_ROOT / ".env").exists():
        load_dotenv(PROJECT_ROOT / ".env")

    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def get_llm_config(config: dict[str, Any], provider: str | None = None) -> dict[str, Any]:
    """获取指定 Provider 的 LLM 调用配置（LiteLLM 模型串格式）。

    config.yaml 中每个 provider 需配置:
        litellm_model: LiteLLM 模型串，如 "deepseek/deepseek-chat"、"ollama/qwen2.5"
        api_key_env:  密钥所在环境变量名；本地模型留空字符串 "" 表示无密钥
        api_base:     （可选）OpenAI 兼容自定义端点

    返回:
        {litellm_model, api_key, temperature, max_retries, provider}，
        配置了 api_base 时额外包含 api_base。

    说明:
        - api_key_env 为空字符串（本地模型，如 ollama）：api_key 返回空串，不报错；
        - api_key_env 非空但环境变量缺失：抛 ValueError，报错信息含环境变量名。
    """
    llm_section = config.get("llm", {})
    provider_name = provider or llm_section.get("default_provider", "deepseek")
    providers = llm_section.get("providers", {})

    if provider_name not in providers:
        raise ValueError(
            f"Provider '{provider_name}' 未在 config.yaml 中配置。"
            f"可用: {list(providers.keys())}"
        )

    p = providers[provider_name]

    # [C 2026-09-09] LiteLLM 模型串为必填项
    litellm_model = p.get("litellm_model")
    if not litellm_model:
        raise ValueError(
            f"Provider '{provider_name}' 缺少 litellm_model 配置"
            f"（LiteLLM 模型串，如 'deepseek/deepseek-chat'、'ollama/qwen2.5'）。"
        )

    # [C 2026-09-09] 本地模型 api_key_env 为空字符串时不查环境变量、不报错
    api_key_env = p.get("api_key_env", "") or ""
    if api_key_env:
        api_key = os.environ.get(api_key_env, "")
        if not api_key:
            raise ValueError(
                f"环境变量 {api_key_env} 未设置。请在 .env 文件中配置该密钥。"
            )
    else:
        api_key = ""

    result: dict[str, Any] = {
        "litellm_model": litellm_model,
        "api_key": api_key,
        "temperature": llm_section.get("temperature", 0.3),
        "max_retries": llm_section.get("max_retries", 2),
        "provider": provider_name,
    }
    # 可选：OpenAI 兼容自定义端点（透传给 ChatLiteLLM 的 api_base）
    if p.get("api_base"):
        result["api_base"] = p["api_base"]
    return result


# [MA 2026-09-08]
# [C 2026-09-09] get_llm_config 适配 LiteLLM 完成
