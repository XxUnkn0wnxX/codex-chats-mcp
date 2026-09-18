"""Setuptools support for stamping the wheel's debug-build capability."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from setuptools.command.editable_wheel import editable_wheel as _editable_wheel
from setuptools.command.build_py import build_py as _build_py

BUILD_DEBUG_ENV = "CODEX_CHATS_BUILD_DEBUG"
_DEBUG_BUILD_MODULE = "_codex_chats_build.py"


def build_debug_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """Return the requested build flavor, accepting only an explicit 0 or 1."""
    values = os.environ if environ is None else environ
    value = values.get(BUILD_DEBUG_ENV, "0")
    if value not in {"0", "1"}:
        raise ValueError(
            f"{BUILD_DEBUG_ENV} must be exactly '0' or '1' when set; got {value!r}."
        )
    return value == "1"


def _build_stamp(debug_build: bool) -> str:
    return (
        '"""Build flavor marker generated during wheel construction."""\n\n'
        f"DEBUG_BUILD = {debug_build}\n"
    )


def write_debug_build_stamp(build_lib: str | Path, debug_build: bool) -> Path:
    """Overwrite the marker in a build directory without touching the source tree."""
    stamp_path = Path(build_lib) / _DEBUG_BUILD_MODULE
    stamp_path.parent.mkdir(parents=True, exist_ok=True)
    stamp_path.write_text(_build_stamp(debug_build), encoding="utf-8")
    return stamp_path


class BuildPy(_build_py):
    """Stamp a wheel-only marker after setuptools copies the Python modules."""

    def run(self) -> None:
        debug_build = build_debug_enabled()
        if self.editable_mode:
            if debug_build:
                raise ValueError(
                    f"{BUILD_DEBUG_ENV}=1 is not supported for editable installs; "
                    "build and install a wheel to enable debug capability."
                )
            super().run()
            return

        super().run()
        # write_text always overwrites this file, including incremental builds
        # that switch from a debug wheel back to the release flavor.
        write_debug_build_stamp(self.build_lib, debug_build)


class EditableWheel(_editable_wheel):
    """Reject debug flavor requests before setuptools creates an editable wheel."""

    def run(self) -> None:
        if build_debug_enabled():
            raise ValueError(
                f"{BUILD_DEBUG_ENV}=1 is not supported for editable installs; "
                "build and install a wheel to enable debug capability."
            )
        super().run()
