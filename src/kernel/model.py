# LiteLLM 统一模型层
"""模型工厂：基于 LiteLLM 统一适配各协议模型（OpenAI 兼容 / Anthropic / Gemini / Ollama 等）。

用法:
    from kernel.config import load_config
    from kernel.model import build_llm, smoke_test

    llm = build_llm(load_config())
    result = llm("只返回JSON：{\"status\":\"ok\"}")  # -> dict（JSON 通道）
    text = llm("写一首短诗", as_text=True)          # -> str（文本通道
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

# 文本通道系统提示词：温和中文约束，不强制 JSON，
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


def _looks_truncated(text: str) -> bool:
    """判断待解析的 JSON 文本是否疑似被 max_tokens 截断。

    两个特征任一命中即判「疑似截断」：
    1. ``{`` 与 ``}`` 数量不配对（对象结构未闭合）；
    2. 字符串引号未闭合（逐字符扫描到末尾仍停在字符串内部，已处理 ``\\`` 转义）。

    仅用于区分错误文案，不改变解析逻辑。

    Args:
        text: 已完成围栏剥离与首尾 ``{}`` 截取后、准备交给 json.loads 的文本。

    Returns:
        True 表示疑似被输出长度上限截断。
    """
    if text.count("{") != text.count("}"):
        return True
    in_string = False
    escaped = False
    for ch in text:
        if escaped:
            escaped = False
        elif ch == "\\":
            escaped = True
        elif ch == '"':
            in_string = not in_string
    return in_string


# 空正文的可执行提示。
# 与 _looks_truncated 同一种情况——输出被长度上限吃掉（真机实测：单次输出 31,046 token 中
# 隐藏思考占 16,805，把 8,192 额度吃光，可见正文为空）。空正文没有可扫描的文本，
# 故改用响应元数据里的等价信号（finish_reason=length，或带 reasoning 段而正文为空），
# 不新增第二套截断判据，也不改解析与重试逻辑。
EMPTY_CONTENT_HINT = "（疑似隐藏思考占满输出额度，可上调 `max_tokens` 或改用非思考模型）"


def _empty_content_hint(response: Any) -> str:
    """正文为空时判断原因是否属于「输出额度被吃掉」，返回可执行提示（否则空串）。

    判定信号（缺失或不符时不加提示，保持原文案）：
    1. 响应元数据 ``finish_reason == "length"``：provider 明确报告因长度上限停止；
    2. ``additional_kwargs`` 里 reasoning 段非空：思考段吃满额度、可见正文为空。

    Args:
        response: ChatLiteLLM.invoke 返回的 AIMessage（或任何带同名属性的对象）。

    Returns:
        EMPTY_CONTENT_HINT 或空串。
    """
    meta = getattr(response, "response_metadata", None)
    if isinstance(meta, dict):
        if str(meta.get("finish_reason") or "").lower() == "length":
            return EMPTY_CONTENT_HINT
    extra = getattr(response, "additional_kwargs", None)
    if isinstance(extra, dict):
        reasoning = extra.get("reasoning_content")
        if isinstance(reasoning, str) and reasoning.strip():
            return EMPTY_CONTENT_HINT
    return ""


def extract_json(content: str, empty_hint: str = "") -> dict[str, Any]:
    """从模型返回文本中健壮提取 JSON 对象。

    处理顺序：
    1. 剥离 ```json ... ``` / ``` ... ``` Markdown 代码围栏；
    2. 若仍有多余说明文字，取第一个 ``{`` 到最后一个 ``}`` 之间的内容；
    3. json.loads 解析，失败抛 NodeExecutionError(node="llm")。

    解析失败时错误信息区分两类：
    满足截断特征（括号不配对 / 引号未闭合）报「疑似被 max_tokens 截断，原始长度 N」，
    否则报常规格式错；二者均为 NodeExecutionError(node="llm")。

    内容为空时，
    原句「模型返回非JSON: 响应内容为空」保留，并在 ``empty_hint`` 非空时追加可执行提示
    （由调用方按 _empty_content_hint 判据给出；缺省空串 = 原文案不变）。

    Args:
        content: 模型返回的原始文本。
        empty_hint: 内容为空时追加到错误文案后的提示，空串表示不追加。

    Returns:
        解析出的 dict。

    Raises:
        NodeExecutionError: 内容为空或无法解析为 JSON 对象。
    """
    if content is None or not str(content).strip():
        raise NodeExecutionError("llm", f"模型返回非JSON: 响应内容为空{empty_hint}")

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
        if _looks_truncated(text):
            # 截断与格式错分开报，便于 runner 定向重试
            raise NodeExecutionError(
                "llm",
                f"模型返回非JSON（疑似被 max_tokens 截断，原始长度 {len(str(content))}）: {preview}",
            ) from exc
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
        - llm(prompt, as_text=True) -> str：文本通道，
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
    # max_tokens 非 None 才透传（未配置时保持 provider 默认）
    if cfg.get("max_tokens") is not None:
        chat_kwargs["max_tokens"] = cfg["max_tokens"]

    chat = ChatLiteLLM(**chat_kwargs)

    def llm(prompt: str, as_text: bool = False) -> Any:
        """调用模型。

        - as_text=False（默认）：系统消息固定约束 JSON 输出，返回 extract_json 解析后的 dict；
        - as_text=True：用温和中文系统提示，不做 JSON 解析，
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
        # 提示只在内容为空时
        # 生效（extract_json 的为空分支），内容非空时该参数不影响任何解析行为
        return extract_json(content, empty_hint=_empty_content_hint(response))
    return llm


def build_chat(
    config: dict[str, Any] | None = None, provider: str | None = None
) -> ChatLiteLLM:
    """构建裸 ChatLiteLLM 实例（不包闭包），供需要 function calling 的节点自行 bind_tools + invoke。

    与 build_llm() 的区别：build_llm 返回 llm(prompt, as_text=False) 闭包（已完成 JSON 解析），
    适用于"只发 prompt 拿结构化输出"的节点；build_chat 返回底层 ChatLiteLLM 实例，
    适用于需要 bind_tools + 多轮 messages 调用的节点（如 feasibility_check 的探针 ReAct 循环）。

    Args:
        config: load_config() 返回的全局配置；None 时自动加载 config.yaml。
        provider: 指定 provider 名称；None 时用 config.yaml 的 default_provider。

    Returns:
        ChatLiteLLM 实例（未绑定工具），调用方据此 self.bind_tools([...]) + self.invoke(messages)。
    """
    if config is None:
        config = load_config()
    cfg = get_llm_config(config, provider)

    chat_kwargs: dict[str, Any] = {
        "model": cfg["litellm_model"],
        "temperature": cfg["temperature"],
        "max_retries": cfg["max_retries"],
    }
    if cfg.get("api_key"):
        chat_kwargs["api_key"] = cfg["api_key"]
    if cfg.get("api_base"):
        chat_kwargs["api_base"] = cfg["api_base"]
    # 与 build_llm 同口径：max_tokens 非 None 才透传
    if cfg.get("max_tokens") is not None:
        chat_kwargs["max_tokens"] = cfg["max_tokens"]

    return ChatLiteLLM(**chat_kwargs)

def smoke_test(llm: Callable[[str], dict]) -> dict[str, Any]:
    """连通性冒烟测试：要求模型只返回 ``{"status": "ok"}``。

    Args:
        llm: build_llm 返回的 llm 闭包。

    Returns:
        模型返回并解析后的 dict，预期为 ``{"status": "ok"}``。
    """
    return llm('只返回JSON：{"status":"ok"}，不要输出任何其他内容。')


