"""演示用的小型服务模块，包含中文注释与嵌套函数。"""

from __future__ import annotations

import functools
from dataclasses import dataclass


@dataclass
class Task:
    """任务记录。"""

    name: str
    priority: int = 0


def with_retry(times: int = 2):
    """装饰器：失败后重试。"""

    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            last_error = None
            for attempt in range(times):
                try:
                    return func(*args, **kwargs)
                except ValueError as exc:  # 只重试可恢复的错误
                    last_error = exc
            raise last_error

        return wrapper

    return decorator


@with_retry(times=3)
def load_tasks(names: list[str]) -> list[Task]:
    """把名称转换成任务，空名称会被跳过。"""

    def normalize(raw: str) -> str:
        # 去掉首尾空白，避免重复任务
        return raw.strip()

    tasks: list[Task] = []
    for name in names:
        cleaned = normalize(name)
        if not cleaned:
            continue
        tasks.append(Task(name=cleaned, priority=len(tasks)))
    return tasks


if __name__ == "__main__":
    print(load_tasks(["a", " b "]))
