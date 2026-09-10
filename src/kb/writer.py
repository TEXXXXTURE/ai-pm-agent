# [C 2026-09-09] M5 知识库接入 - 档案写回（Markdown + index.json，原子写）
"""KBWriter：把决策/评测/案例档案写回本地知识库。

- 档案落盘：store_path/<type>/<id>.md，文件头含 updated / type / id 注释与标题；
- index.json 同步更新（path/title/tags/updated）；
- 所有写入均为原子写：先写同目录 .tmp 临时文件，再 os.replace 覆盖。
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

from kb.store import ARCHIVE_TYPES, read_index


class KBWriter:
    """知识库档案写回器。

    Args:
        store_path: 知识库根目录（与 KBStore 一致）。
    """

    def __init__(self, store_path: str):
        self.store_path = Path(store_path)
        self.index_path = self.store_path / "index.json"

    @staticmethod
    def atomic_overwrite(path: str | Path, text: str) -> Path:
        """原子写文件：先写同目录 <name>.tmp 临时文件，再 os.replace 覆盖目标。

        供 KBWriter 内部及外部复用。
        """
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.parent / (target.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(str(tmp), str(target))
        return target

    def write_back(self, archive: dict) -> Path:
        """写回一条档案并更新 index.json。

        Args:
            archive: 含 id / type(decision|eval|case) / title / content，
                tags 可选（list[str]）。type 非法时抛 ValueError。

        Returns:
            档案文件的 Path。
        """
        archive_type = archive.get("type")
        if archive_type not in ARCHIVE_TYPES:
            raise ValueError(
                f"无效档案类型: {archive_type!r}，必须是 decision / eval / case 之一"
            )

        archive_id = archive["id"]
        title = archive["title"]
        content = archive["content"]
        tags = archive.get("tags") or []
        updated = datetime.now().strftime("%Y-%m-%d")

        target = self.store_path / archive_type / f"{archive_id}.md"
        text = (
            f"<!-- updated: {updated} -->\n"
            f"<!-- type: {archive_type} | id: {archive_id} -->\n"
            f"# {title}\n\n"
            f"{content}\n"
        )
        self.atomic_overwrite(target, text)

        # 更新索引（同样原子写）
        index = read_index(self.index_path)
        index[archive_type][archive_id] = {
            "path": f"{archive_type}/{archive_id}.md",
            "title": title,
            "tags": list(tags),
            "updated": updated,
        }
        self.atomic_overwrite(
            self.index_path,
            json.dumps(index, ensure_ascii=False, indent=2),
        )
        return target


# [C 2026-09-09] M5 KBWriter 实现完成
