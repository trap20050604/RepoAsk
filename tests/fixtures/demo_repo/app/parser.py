"""解析文本配置的模块。"""

from __future__ import annotations

DEFAULT_TIMEOUT = 30


class ConfigParser:
    """把文本配置解析成字典。"""

    supported_formats = ("toml", "ini")

    @property
    def is_empty(self) -> bool:
        """没有任何有效内容时返回 True。"""
        return not self._text.strip()

    def __init__(self, text: str, timeout: int = DEFAULT_TIMEOUT) -> None:
        # 超时时间沿用模块默认值，方便统一调整
        self._text = text
        self.timeout = timeout

    def parse(self) -> dict[str, str]:
        result: dict[str, str] = {}
        for raw_line in self._text.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            key, _, value = line.partition("=")
            result[self.normalize_key(key)] = value.strip()
        return result

    @staticmethod
    def normalize_key(key: str) -> str:
        return key.strip().lower()


def parse_or_default(text: str) -> dict[str, str]:
    """空文本直接返回空字典，避免调用方处理异常。"""
    parser = ConfigParser(text)
    if parser.is_empty:
        return {}
    return parser.parse()
