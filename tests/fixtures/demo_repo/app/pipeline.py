"""把配置和任务串起来的小流程。"""

from __future__ import annotations

from app.parser import parse_or_default
from app.service import load_tasks


def build_tasks(raw_config: str) -> list[str]:
    """从配置文本里读出任务名称并转换成任务。"""
    config = parse_or_default(raw_config)
    names = config.get("tasks", "").split(",")
    return [task.name for task in load_tasks(names)]
