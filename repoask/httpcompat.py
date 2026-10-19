"""与环境变量的小兼容层。

背景（2026-10-19 实测）：本机代理客户端会把 ``NO_PROXY`` 写成
``localhost,127.0.0.1,::1,[::1]``。``httpx`` 构造 Client 时把每个条目当作
URL 模式解析，方括号 IPv6 写法 ``[::1]`` 会抛
``InvalidURL: Invalid port: ':1]'``，于是**任何** ``openai.OpenAI(...)``
都无法创建，聊天与 embedding 两条链路一起崩。

这里只移除方括号写法（``::1`` 本身保留），因此对“本机地址绕过代理”的行为
几乎没有影响；被移除的条目会返回，供探针输出，避免悄悄改变环境。
"""

from __future__ import annotations

import os


def sanitize_no_proxy() -> list[str]:
    """移除 NO_PROXY 中 httpx 无法解析的方括号 IPv6 条目。

    返回被移除的条目列表（没有变化时为空）。可重复调用（幂等）。
    """
    removed: list[str] = []
    # Windows 的环境变量名大小写不敏感，但 os.environ 保留原始写法；
    # 因此按实际键名遍历，避免只处理了大写形式。
    for key in list(os.environ.keys()):
        if key.upper() != "NO_PROXY":
            continue
        raw = os.environ.get(key) or ""
        kept: list[str] = []
        for part in raw.split(","):
            entry = part.strip()
            if not entry:
                continue
            if entry.startswith("[") or entry.endswith("]"):
                removed.append(entry)
                continue
            kept.append(entry)
        os.environ[key] = ",".join(kept)
    return removed
