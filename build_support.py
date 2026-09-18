"""Setuptools support for wheel debug stamps and anonymous source archives."""

from __future__ import annotations

import os
import tarfile
from collections.abc import Mapping
from pathlib import Path

from setuptools.command.editable_wheel import editable_wheel as _editable_wheel
from setuptools.command.build_py import build_py as _build_py
from setuptools.command.sdist import sdist as _sdist

BUILD_DEBUG_ENV = "CODEX_CHATS_BUILD_DEBUG"
_DEBUG_BUILD_MODULE = "_codex_chats_build.py"
_ANONYMOUS_OWNER = "root"
_OWNERSHIP_PAX_FIELDS = frozenset({"uid", "gid", "uname", "gname"})
_TAR_FORMATS = {
    "gztar": ("gz", ".tar.gz"),
    "bztar": ("bz2", ".tar.bz2"),
    "xztar": ("xz", ".tar.xz"),
    "tar": (None, ".tar"),
}


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


def _is_ownership_pax_field(name: str) -> bool:
    return name.lower().rsplit(".", 1)[-1] in _OWNERSHIP_PAX_FIELDS


def anonymize_tarinfo(member: tarfile.TarInfo) -> tarfile.TarInfo:
    """Return a member with canonical neutral ownership and no PAX overrides."""
    member.uid = 0
    member.gid = 0
    member.uname = _ANONYMOUS_OWNER
    member.gname = _ANONYMOUS_OWNER
    member.pax_headers = {
        name: value
        for name, value in member.pax_headers.items()
        if not _is_ownership_pax_field(name)
    }
    return member


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


class Sdist(_sdist):
    """Create tar source archives with ownership independent of the builder."""

    def make_archive(
        self,
        base_name: str | os.PathLike[str],
        format: str,
        root_dir: str | os.PathLike[str] | bytes | os.PathLike[bytes] | None = None,
        base_dir: str | None = None,
        owner: str | None = None,
        group: str | None = None,
    ) -> str:
        if format not in _TAR_FORMATS:
            if format == "ztar":
                raise ValueError("ztar cannot guarantee anonymous ownership; use gztar")
            return super().make_archive(base_name, format, root_dir, base_dir, owner, group)

        compression, extension = _TAR_FORMATS[format]
        archive_base = os.fspath(base_name)
        previous_cwd = os.getcwd()
        if root_dir is not None:
            archive_base = os.path.abspath(archive_base)
            if not self.dry_run:
                os.chdir(root_dir)

        try:
            archive_name = f"{archive_base}{extension}"
            if not self.dry_run:
                Path(archive_name).parent.mkdir(parents=True, exist_ok=True)
                mode = "w" if compression is None else f"w:{compression}"
                with tarfile.open(archive_name, mode) as archive:
                    archive.add(base_dir or os.curdir, filter=anonymize_tarinfo)
            return archive_name
        finally:
            if root_dir is not None:
                os.chdir(previous_cwd)
