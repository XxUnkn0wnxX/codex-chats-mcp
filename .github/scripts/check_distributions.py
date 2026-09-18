"""Validate the exact artifacts retained by CI and uploaded to PyPI."""

from __future__ import annotations

import argparse
import ast
import tarfile
from email.parser import Parser
from pathlib import Path
from zipfile import ZipFile

from packaging.utils import canonicalize_name
from packaging.version import Version


PROJECT_NAME = "codex-chats-mcp-v2"
BUILD_MODULE = "_codex_chats_build.py"


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def check_release_stamp(source: bytes) -> None:
    body = ast.parse(source.decode("utf-8")).body
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        if isinstance(body[0].value.value, str):
            body = body[1:]
    require(len(body) == 1, "Build stamp must contain only its docstring and flag assignment")
    node = body[0]
    if isinstance(node, ast.Assign):
        require(len(node.targets) == 1, "Build stamp must have exactly one target")
        target, value = node.targets[0], node.value
    elif isinstance(node, ast.AnnAssign):
        require(isinstance(node.annotation, ast.Name) and node.annotation.id == "bool", "Unexpected flag annotation")
        target, value = node.target, node.value
    else:
        raise ValueError("Build stamp must be a literal assignment")
    require(isinstance(target, ast.Name) and target.id == "DEBUG_BUILD", "Missing DEBUG_BUILD stamp")
    require(isinstance(value, ast.Constant) and value.value is False, "Debug artifacts cannot be released")


def validate_distributions(
    dist_dir: Path,
    readme: Path,
    expected_name: str = PROJECT_NAME,
    expected_version: str = "",
    release: bool = False,
) -> str:
    require(canonicalize_name(expected_name) == PROJECT_NAME, "Unexpected project name")
    require(not release or bool(expected_version), "A release requires an expected version")
    wheels = list(dist_dir.glob("*.whl"))
    sdists = list(dist_dir.glob("*.tar.gz"))
    require(len(wheels) == len(sdists) == 1, "Expected one wheel and one source archive")
    require(len(list(dist_dir.iterdir())) == 2, "Unexpected files in distribution directory")

    with ZipFile(wheels[0]) as wheel:
        metadata_paths = [name for name in wheel.namelist() if name.endswith(".dist-info/METADATA")]
        require(len(metadata_paths) == 1, "Expected one wheel metadata file")
        wheel_metadata = Parser().parsestr(wheel.read(metadata_paths[0]).decode("utf-8"))
        check_release_stamp(wheel.read(BUILD_MODULE))

    with tarfile.open(sdists[0], "r:gz") as sdist:
        metadata_paths = [
            member for member in sdist.getmembers()
            if len(Path(member.name).parts) == 2 and member.name.endswith("/PKG-INFO")
        ]
        build_paths = [
            member for member in sdist.getmembers()
            if len(Path(member.name).parts) == 2 and member.name.endswith(f"/{BUILD_MODULE}")
        ]
        require(len(metadata_paths) == len(build_paths) == 1, "Missing or ambiguous sdist metadata/stamp")
        require(metadata_paths[0].isfile() and build_paths[0].isfile(), "Metadata must be regular files")
        with sdist.extractfile(metadata_paths[0]) as metadata_file:
            sdist_metadata = Parser().parsestr(metadata_file.read().decode("utf-8"))
        with sdist.extractfile(build_paths[0]) as build_file:
            check_release_stamp(build_file.read())

    version = expected_version or wheel_metadata["Version"]
    parsed_version = Version(version)
    if release:
        require(
            not (parsed_version.is_prerelease or parsed_version.is_devrelease or parsed_version.local),
            "PyPI releases require a stable version without a local suffix",
        )
    expected_readme = readme.read_text(encoding="utf-8").strip()
    for label, metadata in (("wheel", wheel_metadata), ("sdist", sdist_metadata)):
        require(canonicalize_name(metadata["Name"]) == PROJECT_NAME, f"Wrong {label} project name")
        require(metadata["Version"] == version, f"Wrong {label} version")
        require(metadata["Description-Content-Type"] == "text/markdown", f"Wrong {label} README format")
        require(bool(metadata["Summary"]), f"Missing {label} PyPI summary")
        require(metadata.get_payload().strip() == expected_readme, f"Stale {label} README")
    return version


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist-dir", type=Path, default=Path("dist"))
    parser.add_argument("--readme", type=Path, default=Path("README.md"))
    parser.add_argument("--expected-name", default=PROJECT_NAME)
    parser.add_argument("--expected-version", default="")
    parser.add_argument("--release", action="store_true")
    args = parser.parse_args()
    version = validate_distributions(**vars(args))
    print(f"Validated {PROJECT_NAME} {version}: release stamps, matching metadata and README")


if __name__ == "__main__":
    main()
