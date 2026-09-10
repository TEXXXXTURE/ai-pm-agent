# [C 2026-09-08] M2 组件框架 - 组件注册表
"""ComponentRegistry：自动发现并加载 prompts / schemas / tools / guards 组件。

扫描约定：
- prompts/*.md        -> 文件名（不含扩展名）作为 key
- schemas/*.py        -> 文件名作为 key，类名 = 文件名大驼峰 + "Schema"
- tools/*.py          -> 文件名作为 key（预留，本里程碑无工具）
- guards/*.py         -> 函数名作为 key（扫描模块内定义的顶层函数）
"""
from __future__ import annotations

import importlib.util
import inspect
import sys
from pathlib import Path
from typing import Any, Callable

from jinja2 import Template
from pydantic import BaseModel


def _to_schema_class_name(filename: str) -> str:
    """文件名转 schema 类名：intake -> IntakeSchema, needs_discovery -> NeedsDiscoverySchema。"""
    return "".join(part.capitalize() for part in filename.split("_")) + "Schema"


class ComponentRegistry:
    """组件注册表。

    Args:
        base_dir: components 目录路径（绝对或相对均可）。
    """

    def __init__(self, base_dir: str):
        self.base_dir = Path(base_dir)
        self._prompts: dict[str, Path] = {}
        self._schemas: dict[str, Path] = {}
        self._tools: dict[str, Path] = {}
        self._guards: dict[str, Callable] = {}
        self._scan()

    # ────────────────── 扫描 ──────────────────
    def _scan(self) -> None:
        """扫描 4 个子目录并注册到内部字典。"""
        self._scan_prompts()
        self._scan_schemas()
        self._scan_tools()
        self._scan_guards()

    def _scan_prompts(self) -> None:
        prompts_dir = self.base_dir / "prompts"
        if not prompts_dir.is_dir():
            return
        for f in prompts_dir.glob("*.md"):
            self._prompts[f.stem] = f

    def _scan_schemas(self) -> None:
        schemas_dir = self.base_dir / "schemas"
        if not schemas_dir.is_dir():
            return
        for f in schemas_dir.glob("*.py"):
            if f.name.startswith("_"):
                continue
            self._schemas[f.stem] = f

    def _scan_tools(self) -> None:
        tools_dir = self.base_dir / "tools"
        if not tools_dir.is_dir():
            return
        for f in tools_dir.glob("*.py"):
            if f.name.startswith("_"):
                continue
            self._tools[f.stem] = f

    def _scan_guards(self) -> None:
        """加载每个 guard 模块，按函数名注册（仅本模块定义的顶层函数）。"""
        guards_dir = self.base_dir / "guards"
        if not guards_dir.is_dir():
            return
        for f in guards_dir.glob("*.py"):
            if f.name.startswith("_"):
                continue
            module_name = f"components_guards_{f.stem}"
            module = self._load_module(module_name, f)
            for attr_name in dir(module):
                if attr_name.startswith("_"):
                    continue
                attr = getattr(module, attr_name)
                if inspect.isfunction(attr) and attr.__module__ == module.__name__:
                    self._guards[attr_name] = attr

    # ────────────────── 动态加载工具 ──────────────────
    @staticmethod
    def _load_module(module_name: str, file_path: Path):
        """从文件路径动态加载 Python 模块。"""
        spec = importlib.util.spec_from_file_location(module_name, file_path)
        if spec is None or spec.loader is None:
            raise ImportError(f"无法加载模块: {file_path}")
        module = importlib.util.module_from_spec(spec)
        # 先注册到 sys.modules 再执行：pydantic 解析同模块内模型前向引用
        # （如 PRDOutputSchema -> PrdSection）时依赖 sys.modules[__module__]。
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        return module
        # [C 2026-09-09] M6 动态加载模块注册 sys.modules，修复 pydantic 前向引用解析

    # ────────────────── 公开 API ──────────────────
    def load_prompt(self, name: str) -> Template:
        """读取 prompts/{name}.md，返回 Jinja2 Template。"""
        if name not in self._prompts:
            raise KeyError(f"prompt '{name}' 未注册")
        text = self._prompts[name].read_text(encoding="utf-8")
        return Template(text)

    def read_prompt(self, name: str) -> str:
        """读取 prompts/{name}.md 的原始文本（不包装成 Template）。

        未注册时抛 KeyError。NodeRunner 内部会自行用 Jinja2 渲染，
        因此节点接线时传原始字符串即可。
        """
        if name not in self._prompts:
            raise KeyError(f"prompt '{name}' 未注册")
        return self._prompts[name].read_text(encoding="utf-8")
        # [C 2026-09-09] M6 新增 read_prompt：返回 prompt 原始文本供 NodeSpec 使用

    def load_schema(self, name: str) -> type:
        """从 schemas/{name}.py 动态导入，返回 Pydantic Model 类。

        查找策略：
        1. 优先按约定类名（文件名大驼峰 + "Schema"，如 intake -> IntakeSchema）查找；
        2. 约定名不存在时，回退扫描模块内定义的 BaseModel 子类。
        """
        if name not in self._schemas:
            raise KeyError(f"schema '{name}' 未注册")
        module_name = f"components_schemas_{name}"
        module = self._load_module(module_name, self._schemas[name])

        # 策略 1：约定类名
        class_name = _to_schema_class_name(name)
        if hasattr(module, class_name):
            return getattr(module, class_name)

        # 策略 2：扫描 BaseModel 子类（约定名不适用时回退，如 UserInsightsSchema / PRDOutputSchema）
        for attr_name in dir(module):
            attr = getattr(module, attr_name)
            if (
                inspect.isclass(attr)
                and issubclass(attr, BaseModel)
                and attr is not BaseModel
                and attr.__module__ == module.__name__
            ):
                return attr

        raise AttributeError(
            f"模块 {name} 中未找到 schema 类（约定名 {class_name} 不存在，且无 BaseModel 子类）"
        )

    def load_guards(self, names: list[str]) -> list[Callable]:
        """按函数名返回 guard 可调用对象列表。"""
        result: list[Callable] = []
        for name in names:
            if name not in self._guards:
                raise KeyError(f"guard '{name}' 未注册")
            result.append(self._guards[name])
        return result

    def get_registered(self) -> dict[str, list[str]]:
        """返回已注册的组件清单。"""
        return {
            "prompts": sorted(self._prompts.keys()),
            "schemas": sorted(self._schemas.keys()),
            "tools": sorted(self._tools.keys()),
            "guards": sorted(self._guards.keys()),
        }


# [C 2026-09-08] registry.py 实现完成
