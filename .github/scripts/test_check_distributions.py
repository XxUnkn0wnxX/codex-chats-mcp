import io
import tarfile
import tempfile
import unittest
from pathlib import Path
from zipfile import ZipFile

from check_distributions import check_release_stamp, validate_distributions


class DistributionValidationTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.dist = self.root / "dist"
        self.dist.mkdir()
        self.readme = self.root / "README.md"
        self.readme.write_text("# codex-chats-mcp\n\nA fork — with Markdown.\n", encoding="utf-8")

    def _archives(
        self,
        *,
        version: str = "0.2.0",
        wheel_name: str = "codex-chats-mcp-v2",
        sdist_name: str = "codex-chats-mcp-v2",
        sdist_version: str | None = None,
        wheel_stamp: bytes = b"DEBUG_BUILD = False\n",
        sdist_stamp: bytes = b"DEBUG_BUILD = False\n",
        readme: str | None = None,
        summary: str = "An unofficial MCP server",
        content_type: str = "text/markdown",
        uid: int = 0,
        gid: int = 0,
        uname: str = "root",
        gname: str = "root",
        pax_headers: dict[str, str] | None = None,
    ) -> None:
        body = readme if readme is not None else self.readme.read_text(encoding="utf-8")

        def metadata(name: str, package_version: str) -> bytes:
            return (
                f"Metadata-Version: 2.4\nName: {name}\nVersion: {package_version}\n"
                f"Summary: {summary}\nDescription-Content-Type: {content_type}\n\n{body}"
            ).encode("utf-8")

        with ZipFile(self.dist / "test.whl", "w") as wheel:
            wheel.writestr("test.dist-info/METADATA", metadata(wheel_name, version))
            wheel.writestr("_codex_chats_build.py", wheel_stamp)
        with tarfile.open(self.dist / "test.tar.gz", "w:gz") as sdist:
            for name, data in (
                ("test/PKG-INFO", metadata(sdist_name, sdist_version or version)),
                ("test/_codex_chats_build.py", sdist_stamp),
            ):
                entry = tarfile.TarInfo(name)
                entry.size = len(data)
                entry.uid = uid
                entry.gid = gid
                entry.uname = uname
                entry.gname = gname
                entry.pax_headers = dict(pax_headers or {})
                sdist.addfile(entry, io.BytesIO(data))

    def test_valid_release_preserves_unicode_readme(self) -> None:
        self._archives()
        self.assertEqual(
            validate_distributions(self.dist, self.readme, expected_version="0.2.0", release=True),
            "0.2.0",
        )

    def test_normalized_project_name(self) -> None:
        self._archives()
        self.assertEqual(validate_distributions(self.dist, self.readme, "codex_chats_mcp_v2"), "0.2.0")

    def test_development_artifacts_are_allowed_but_cannot_be_published(self) -> None:
        for version in ("0.2.1.dev1", "0.2.1rc1", "0.2.1+local"):
            with self.subTest(version=version):
                self._archives(version=version)
                self.assertEqual(validate_distributions(self.dist, self.readme), version)
                with self.assertRaises(ValueError):
                    validate_distributions(self.dist, self.readme, expected_version=version, release=True)

    def test_release_requires_explicit_expected_version(self) -> None:
        self._archives()
        with self.assertRaises(ValueError):
            validate_distributions(self.dist, self.readme, release=True)

    def test_wrong_expected_name_or_version_rejected(self) -> None:
        self._archives()
        for kwargs in ({"expected_name": "codex-chats-mcp"}, {"expected_version": "0.2.1"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                validate_distributions(self.dist, self.readme, **kwargs)

    def test_mismatched_archive_identity_rejected(self) -> None:
        for kwargs in (
            {"wheel_name": "another-package"},
            {"sdist_name": "codex-chats-mcp"},
            {"sdist_version": "0.2.1"},
        ):
            with self.subTest(kwargs=kwargs):
                self._archives(**kwargs)
                with self.assertRaises(ValueError):
                    validate_distributions(self.dist, self.readme)

    def test_debug_stamp_in_either_archive_rejected(self) -> None:
        for archive in ("wheel_stamp", "sdist_stamp"):
            with self.subTest(archive=archive):
                self._archives(**{archive: b"DEBUG_BUILD = True\n"})
                with self.assertRaises(ValueError):
                    validate_distributions(self.dist, self.readme)

    def test_stale_readme_or_invalid_description_rejected(self) -> None:
        for kwargs in ({"readme": "old README"}, {"summary": ""}, {"content_type": "text/plain"}):
            with self.subTest(kwargs=kwargs):
                self._archives(**kwargs)
                with self.assertRaises(ValueError):
                    validate_distributions(self.dist, self.readme)

    def test_extra_artifacts_rejected(self) -> None:
        self._archives()
        (self.dist / "debug.whl").touch()
        with self.assertRaises(ValueError):
            validate_distributions(self.dist, self.readme)

    def test_non_anonymous_sdist_ownership_and_pax_overrides_rejected(self) -> None:
        cases = (
            {"uid": 1},
            {"gid": 1},
            {"uname": "untrusted-owner"},
            {"gname": "untrusted-group"},
            {"pax_headers": {"uid": "1"}},
            {"pax_headers": {"SCHILY.gid": "1"}},
            {"pax_headers": {"LIBARCHIVE.uname": "untrusted-owner"}},
            {"pax_headers": {"VENDOR.gname": "untrusted-group"}},
        )
        for kwargs in cases:
            with self.subTest(kwargs=kwargs):
                self._archives(**kwargs)
                with self.assertRaises(ValueError):
                    validate_distributions(self.dist, self.readme)

    def test_missing_archives_rejected(self) -> None:
        with self.assertRaises(ValueError):
            validate_distributions(self.dist, self.readme)

    def test_missing_duplicate_and_non_boolean_stamps_rejected(self) -> None:
        for source in (
            b"OTHER_FLAG = False",
            b"DEBUG_BUILD = False\nDEBUG_BUILD = True",
            b"DEBUG_BUILD = 0",
            b"DEBUG_BUILD = 'False'",
        ):
            with self.subTest(source=source), self.assertRaises(ValueError):
                check_release_stamp(source)

    def test_annotated_release_stamp(self) -> None:
        check_release_stamp(b"DEBUG_BUILD: bool = False")

    def test_executable_or_indirect_stamp_mutations_rejected(self) -> None:
        for source in (
            b"DEBUG_BUILD = False\nDEBUG_BUILD |= True",
            b"DEBUG_BUILD = False\ndel DEBUG_BUILD",
            b"DEBUG_BUILD = False\nif True:\n    DEBUG_BUILD = True",
            b"DEBUG_BUILD = False\nexec('DEBUG_BUILD = True')",
            b"DEBUG_BUILD = OTHER_FLAG = False",
            b"DEBUG_BUILD: evaluate_annotation() = False",
            b"DEBUG_BUILD = bool(0)",
        ):
            with self.subTest(source=source), self.assertRaises(ValueError):
                check_release_stamp(source)


if __name__ == "__main__":
    unittest.main()
