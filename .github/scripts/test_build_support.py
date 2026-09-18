"""Build-tooling-only tests for the wheel build-flavor stamp."""

from __future__ import annotations

import importlib.util
import tarfile
import tempfile
import unittest
from pathlib import Path
from types import ModuleType

from setuptools import Distribution

SOURCE_ROOT = Path(__file__).resolve().parents[2]
BUILD_SUPPORT_PATH = SOURCE_ROOT / "build_support.py"


def _load_build_support() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "codex_chats_mcp_build_support_test", BUILD_SUPPORT_PATH
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load {BUILD_SUPPORT_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


build_support = _load_build_support()


class BuildSupportTest(unittest.TestCase):
    def test_build_flag_defaults_to_release_and_rejects_other_values(self) -> None:
        self.assertFalse(build_support.build_debug_enabled({}))
        self.assertFalse(
            build_support.build_debug_enabled(
                {build_support.BUILD_DEBUG_ENV: "0"}
            )
        )
        self.assertTrue(
            build_support.build_debug_enabled(
                {build_support.BUILD_DEBUG_ENV: "1"}
            )
        )

        for value in ("", "true", "yes", "2", " 1", "1 "):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "CODEX_CHATS_BUILD_DEBUG"):
                    build_support.build_debug_enabled(
                        {build_support.BUILD_DEBUG_ENV: value}
                    )

    def test_incremental_stamp_is_overwritten_without_mutating_source_marker(self) -> None:
        source_marker = SOURCE_ROOT / "_codex_chats_build.py"
        original_source = source_marker.read_text(encoding="utf-8")

        with tempfile.TemporaryDirectory() as temp_dir:
            first_stamp = build_support.write_debug_build_stamp(temp_dir, True)
            self.assertEqual(first_stamp.name, "_codex_chats_build.py")
            self.assertIn("DEBUG_BUILD = True", first_stamp.read_text(encoding="utf-8"))

            second_stamp = build_support.write_debug_build_stamp(temp_dir, False)
            self.assertEqual(second_stamp, first_stamp)
            stamped_source = second_stamp.read_text(encoding="utf-8")
            self.assertIn("DEBUG_BUILD = False", stamped_source)
            self.assertNotIn("DEBUG_BUILD = True", stamped_source)

        self.assertEqual(source_marker.read_text(encoding="utf-8"), original_source)

    def test_tar_filter_canonicalizes_ownership_and_strips_pax_overrides(self) -> None:
        member = tarfile.TarInfo("package/file.py")
        member.uid = 123
        member.gid = 456
        member.uname = "untrusted-owner"
        member.gname = "untrusted-group"
        member.pax_headers = {
            "uid": "123",
            "SCHILY.gid": "456",
            "LIBARCHIVE.uname": "untrusted-owner",
            "VENDOR.gname": "untrusted-group",
            "path": "package/file.py",
        }

        result = build_support.anonymize_tarinfo(member)

        self.assertIs(result, member)
        self.assertEqual((result.uid, result.gid), (0, 0))
        self.assertEqual((result.uname, result.gname), ("root", "root"))
        self.assertEqual(result.pax_headers, {"path": "package/file.py"})

    def test_gztar_archive_respects_root_dir_and_has_neutral_ownership(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root_dir = Path(temp_dir) / "root"
            source_dir = root_dir / "package"
            source_dir.mkdir(parents=True)
            (source_dir / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
            base_name = Path(temp_dir) / "dist" / "source"
            command = build_support.Sdist(Distribution())
            command.dry_run = False
            previous_cwd = Path.cwd()

            archive_name = command.make_archive(
                base_name,
                "gztar",
                root_dir=root_dir,
                base_dir="package",
                owner="untrusted-owner",
                group="untrusted-group",
            )

            self.assertEqual(Path.cwd(), previous_cwd)
            with tarfile.open(archive_name, "r:gz") as archive:
                members = archive.getmembers()
            self.assertTrue(members)
            self.assertTrue(all(member.name.startswith("package") for member in members))
            self.assertTrue(
                all(
                    (member.uid, member.gid, member.uname, member.gname)
                    == (0, 0, "root", "root")
                    for member in members
                )
            )

    def test_gztar_dry_run_creates_no_archive_and_restores_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root_dir = Path(temp_dir) / "root"
            (root_dir / "package").mkdir(parents=True)
            base_name = Path(temp_dir) / "dist" / "source"
            command = build_support.Sdist(Distribution())
            command.dry_run = True
            previous_cwd = Path.cwd()

            archive_name = command.make_archive(
                base_name, "gztar", root_dir=root_dir, base_dir="package"
            )

            self.assertEqual(Path.cwd(), previous_cwd)
            self.assertEqual(Path(archive_name), Path(f"{base_name}.tar.gz"))
            self.assertFalse(Path(archive_name).exists())


if __name__ == "__main__":
    unittest.main()
