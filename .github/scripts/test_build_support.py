"""Build-tooling-only tests for the wheel build-flavor stamp."""

from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path
from types import ModuleType

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


if __name__ == "__main__":
    unittest.main()
