# [C 2026-09-09] M0 LiteLLM 统一模型层
"""模型工厂：基于 LiteLLM 统一适配各协议模型（OpenAI 兼容 / Anthropic / Gemini / Ollama 等）。

用法:
    from kernel.config import load_config
    from kernel.model import build_llm, smoke_test

    llm = build_llm(load_config())
    result = llm("只返回JSON：{\"status\":\"ok\"}")  # -> dict（JSON 通道）
    text = llm("写一首短诗", as_text=True)          # -> str（文本通道，[C 2026-09-09] T1）
    smoke_test(llm)  # -> {"status": "ok"}

llm 契约与 NodeRunner 约定一致：
- llm(prompt: str) -> dict             JSON 通道（默认）：系统提示约束只输出 JSON，返回解析后的 dict；
- llm(prompt: str, as_text=True) -> str  文本通道（T1）：温和中文系统提示，返回模型原文 str（strip 后），不做 JSON 解析。
"""
from __future__ import annotations

import json
import re
from typing import Any, Callable

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_litellm import ChatLiteLLM

from kernel.config import get_llm_config, load_config
from kernel.exceptions import NodeExecutionError

# 系统提示词：约束模型只输出 JSON
SYSTEM_PROMPT = "你是AI产品经理助手，只输出JSON，不要输出任何多余文字、解释或Markdown代码围栏。"

# 文本通道系统提示词（[C 2026-09-09] T1）：温和中文约束，不强制 JSON，
# 用于模型原生输出 Markdown 全文等"文本即产物"的场景
SYSTEM_PROMPT_TEXT = "你是AI产品经理助手，用中文回答，严格按用户要求的格式输出。"

# JSON 代码围栏正则：```json ... ``` 或 ``` ... ```
_FENCE_RE = re.compile(r"```(?:[a-zA-Z0-9_+-]*)?\s*(.*?)```", re.DOTALL)


def _join_text_blocks(content: Any) -> str:
    """把模型返回内容拼接为纯文本。

    部分模型/多模态返回内容块列表（list[dict]，文本在 block["text"]），
    逐块拼接；纯字符串/其他类型原样 str() 化。JSON 通道与文本通道共用。
    """
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                parts.append(str(block.get("text", "")))
            else:
                parts.append(str(block))
        return "".join(parts)
    return str(content)


def extract_json(content: str) -> dict[str, Any]:
    """从模型返回文本中健壮提取 JSON 对象。

    处理顺序：
    1. 剥离 ```json ... ``` / ``` ... ``` Markdown 代码围栏；
    2. 若仍有多余说明文字，取第一个 ``{`` 到最后一个 ``}`` 之间的内容；
    3. json.loads 解析，失败抛 NodeExecutionError(node="llm")。

    Args:
        content: 模型返回的原始文本。

    Returns:
        解析出的 dict。

    Raises:
        NodeExecutionError: 内容为空或无法解析为 JSON 对象。
    """
    if content is None or not str(content).strip():
        raise NodeExecutionError("llm", "模型返回非JSON: 响应内容为空")

    text = str(content).strip()

    # 1. 剥离代码围栏（取最后一个围栏块，兼容模型先写说明再给围栏的情况）
    fences = _FENCE_RE.findall(text)
    if fences:
        text = fences[-1].strip()

    # 2. 提取第一个 { 到最后一个 } 之间的内容
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        text = text[start : end + 1]

    # 3. 解析
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        preview = str(content)[:200].replace("\n", " ")
        raise NodeExecutionError("llm", f"模型返回非JSON: {preview}") from exc

    if not isinstance(data, dict):
        preview = str(content)[:200].replace("\n", " ")
        raise NodeExecutionError("llm", f"模型返回的JSON不是对象: {preview}")

    return data


def build_llm(
    config: dict[str, Any] | None = None, provider: str | None = None
) -> Callable[..., Any]:
    """构建 LLM 调用闭包。

    内部通过 get_llm_config 读取 LiteLLM 模型串与密钥，
    返回与 NodeRunner 契约一致的 llm(prompt, as_text=False)。

    Args:
        config: load_config() 返回的全局配置；None 时自动加载 config.yaml。
        provider: 指定 provider 名称；None 时用 config.yaml 的 default_provider。

    Returns:
        llm 闭包：
        - llm(prompt) -> dict：JSON 通道（默认），已完成 JSON 解析；
        - llm(prompt, as_text=True) -> str：文本通道（[C 2026-09-09] T1），
          模型原文 strip 后直接返回，不做 JSON 解析。
    """
    if config is None:
        config = load_config()
    cfg = get_llm_config(config, provider)

    chat_kwargs: dict[str, Any] = {
        "model": cfg["litellm_model"],
        "temperature": cfg["temperature"],
        "max_retries": cfg["max_retries"],
    }
    # 本地模型（如 ollama，api_key_env 为空）不传 api_key；
    # 云端模型密钥缺失时 get_llm_config 已报错，走到这里 api_key 必非空
    if cfg.get("api_key"):
        chat_kwargs["api_key"] = cfg["api_key"]
    # 可选：OpenAI 兼容自定义端点（config.yaml 中的 api_base）
    if cfg.get("api_base"):
        chat_kwargs["api_base"] = cfg["api_base"]

    chat = ChatLiteLLM(**chat_kwargs)

    def llm(prompt: str, as_text: bool = False) -> Any:
        """调用模型。

        - as_text=False（默认）：系统消息固定约束 JSON 输出，返回 extract_json 解析后的 dict；
        - as_text=True（[C 2026-09-09] T1）：用温和中文系统提示，不做 JSON 解析，
          内容块拼接后 strip 直接返回 str（模型原生 Markdown 全文走此通道）。
        """
        system_prompt = SYSTEM_PROMPT_TEXT if as_text else SYSTEM_PROMPT
        messages = [
            SystemMessage(content=system_prompt),
            HumanMessage(content=prompt),
        ]
        response = chat.invoke(messages)
        content = _join_text_blocks(response.content)
        if as_text:
            return content.strip()
        return extract_json(content)
        # [C 2026-09-09] T1 llm 增加 as_text 文本通道：JSON 通道行为保持不变

    return llm


def smoke_test(llm: Callable[[str], dict]) -> dict[str, Any]:
    """连通性冒烟测试：要求模型只返回 ``{"status": "ok"}``。

    Args:
        llm: build_llm 返回的 llm 闭包。

    Returns:
        模型返回并解析后的 dict，预期为 ``{"status": "ok"}``。
    """
    return llm('只返回JSON：{"status":"ok"}，不要输出任何其他内容。')


# [C 2026-09-09] model.py 实现完成
