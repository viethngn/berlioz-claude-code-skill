"""Dependency-check helper for llm-wiki scripts.

Every non-stdlib script starts with:

    from _deps import require
    require(["requests", "markdownify"])

If any listed module cannot be imported, print a friendly message pointing
at install.sh and exit(1) — no Python traceback surfaces to the user.
"""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path
from typing import Iterable, Mapping


PIP_NAMES: Mapping[str, str] = {
    "requests": "requests",
    "markdownify": "markdownify",
    "bs4": "beautifulsoup4",
    "pypdf": "pypdf",
    "docx": "python-docx",
    "openpyxl": "openpyxl",
    "pptx": "python-pptx",
    "google.genai": "google-genai",
    "PIL": "pillow",
}


def _plugin_root() -> Path:
    return Path(__file__).resolve().parents[3]


def require(modules: Iterable[str]) -> None:
    missing: list[str] = []
    for module in modules:
        try:
            importlib.import_module(module)
        except ImportError:
            missing.append(module)

    if not missing:
        return

    plugin_root = _plugin_root()
    install_sh = plugin_root / "install.sh"
    setup_md = (
        plugin_root
        / "skills"
        / "ingest"
        / "references"
        / "setup.md"
    )

    pip_names = " ".join(PIP_NAMES.get(m, m) for m in missing)

    lines = [
        "",
        "ERROR: Missing Python dependencies for the llm-wiki plugin:",
        "",
    ]
    for module in missing:
        lines.append(f"  - {module} (install as: {PIP_NAMES.get(module, module)})")
    lines.extend(
        [
            "",
            "Fix — run the installer once:",
            f"    bash {install_sh}",
            "",
            "Or install manually:",
            f"    python3 -m pip install --user {pip_names}",
            "",
            f"For offline / mirrored networks, see: {setup_md}",
            "",
        ]
    )
    print("\n".join(lines), file=sys.stderr)
    sys.exit(1)


def script_dir() -> Path:
    return Path(__file__).resolve().parent


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        print("Usage: _deps.py <module> [<module> ...]", file=sys.stderr)
        sys.exit(2)
    require(args)
    print("OK: all requested modules are importable")
