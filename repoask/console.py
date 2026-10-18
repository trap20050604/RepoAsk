"""Console output helpers.

Python picks the locale encoding for stdio, which on Chinese Windows is cp936.
Chinese text then turns into mojibake the moment output is piped or captured.
Reconfiguring to UTF-8 keeps CLI and script output stable.
"""

from __future__ import annotations

import sys


def force_utf8_streams() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            continue
