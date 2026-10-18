"""Project-local pytest configuration.

Why ``basetemp`` is redirected: this machine's default temporary root
(``%TEMP%\\pytest-of-ncp``) currently denies access, so every test using
``tmp_path`` failed during fixture setup with ``PermissionError: [WinError 5]``.
Pointing the temporary root inside the project keeps the suite runnable without
changing machine-wide permissions. Delete ``.pytest-tmp`` at any time; it is
ignored by git.

Pass ``--basetemp=<path>`` explicitly to override this.
"""

from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def pytest_configure(config) -> None:
    if config.option.basetemp is None:
        config.option.basetemp = str(PROJECT_ROOT / ".pytest-tmp")
