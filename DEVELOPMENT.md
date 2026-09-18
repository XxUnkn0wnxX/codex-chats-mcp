# Development guide

This guide is for the maintained fork's `develop` source line. Commands use zsh and assume the repository root is the current directory.

## 1. Clone `develop` and set local policy

Use Python 3.10 or newer. Python 3.13 is the reproducible target below. The maintained fork is `origin`; the original project is `upstream`:

```zsh
set -euo pipefail
git clone --branch develop https://github.com/XxUnkn0wnxX/codex-chats-mcp.git
cd codex-chats-mcp
git remote add upstream https://github.com/shoyu-ramen/codex-chats-mcp.git
git fetch --prune --all
git switch develop
git status --short --branch
```

Keep development, review, and pushes on `develop`; review upstream changes before selectively integrating them. Put private workstation-only paths in the clone-local `.git/info/exclude`. Standard project artifacts such as `.venv/` remain in the shared `.gitignore`. Print the local exclude-file path:

```zsh
set -euo pipefail
git rev-parse --git-path info/exclude
```

Add these lines to that file, preserving its existing contents. Clone-local exclusions do not travel with the repository, so repeat this in each fresh clone:

```gitignore
/tmp/
/AGENTS.md
```

Check coverage before creating temporary files:

```zsh
git check-ignore -v --no-index tmp/ AGENTS.md .venv/
```

## 2. Install an editable checkout

Use a fresh directory and one isolated virtual environment per install method; choose one method rather than reusing an environment from another distribution. Do not co-install the upstream `codex-chats-mcp` distribution and this fork: they provide the same `codex_chats_mcp` module and `codex-chats-mcp` executable, so one installation can mask or overwrite the other.

Pin MCP first, then install this project without letting its dependency range upgrade MCP:

```zsh
set -euo pipefail
python3.13 --version
python3.13 -m venv .venv
source .venv/bin/activate
python -m pip install 'mcp[cli]==2.1.1'
python -m pip install --no-deps -e .
python -m pip check
```

For a fresh directory and separate virtual environment, install a snapshot of the current fork `develop` HEAD directly from Git instead of an editable checkout:

```zsh
set -euo pipefail
python3.13 -m venv .venv
source .venv/bin/activate
python -m pip install git+https://github.com/XxUnkn0wnxX/codex-chats-mcp.git@develop
python -m pip check
```

The fixtures use temporary fake authentication data. Never read, print, copy, or edit the real `~/.codex/auth.json`.

## 3. Run local tests

```zsh
set -euo pipefail
python -m compileall -q codex_chats_mcp.py tests
python -m unittest discover -s tests -p 'test*.py' -v
python -m pip check
git diff --check
```

Full discovery includes the MCP v2 `initialize`/`list_tools` stdio handshake, which verifies 17 advertised tools. Running the executable directly waits for an MCP client on stdio; it is not an interactive test.

Build and artifact-validation tooling has its own tests (not runtime dependencies):

```zsh
python -m pip install build twine packaging 'setuptools>=77.0.3'
python -m unittest discover -s .github/scripts -p 'test*.py' -v
```

## 4. Test a clean wheel

This fail-fast subshell captures `repo_root` before changing directory, builds into project-local ignored `tmp/`, tests from an empty cwd, and propagates failures. The metadata check proves the wheel, not the checkout, is imported:

```zsh
(
  set -euo pipefail
  repo_root="$(pwd -P)"
  mkdir -p "$repo_root/tmp"
  build_dir="$(mktemp -d "$repo_root/tmp/clean-wheel.XXXXXX")"
  CODEX_CHATS_BUILD_DEBUG=0 python3.13 -m pip wheel --no-deps --wheel-dir "$build_dir" "$repo_root"
  wheel_path="$(print -r -- "$build_dir"/*.whl)"
  test -f "$wheel_path"
  python3.13 -m venv "$build_dir/venv"
  venv_python="$build_dir/venv/bin/python"
  "$venv_python" -m pip install 'mcp[cli]==2.1.1'
  "$venv_python" -m pip install --no-deps "$wheel_path"
  "$venv_python" -m pip check
  empty_cwd="$build_dir/empty-cwd"
  mkdir "$empty_cwd"
  (
    cd "$empty_cwd"
    "$venv_python" -m unittest discover -s "$repo_root/tests" -p 'test*.py' -v
    "$venv_python" - <<'PY'
from importlib.metadata import version
from pathlib import Path
import codex_chats_mcp
import mcp
assert version("codex-chats-mcp-v2") == "0.2.0"
assert version("mcp") == "2.1.1"
assert codex_chats_mcp.DEBUG_BUILD is False
for name, module in (("codex_chats_mcp", codex_chats_mcp), ("mcp", mcp)):
    module_path = Path(module.__file__).resolve()
    assert "site-packages" in module_path.parts, module_path
    print(f"{name}: {module_path}")
PY
  )
)
```

## 5. Rebuild the live venv

Build the wheel before changing the live installation. This example explicitly produces a release build with diagnostic file logging disabled. `CODEX_HOME` defaults to `$HOME/.codex`; `CODEX_CHATS_INSTALL_DIR` can override the install directory. The target itself is the venv, so do not create a nested `.venv`. Any existing installation is moved to a private sibling backup first; no old files are mixed into the fresh venv. Reload active clients after rebuilding.

```zsh
set -euo pipefail
repo_root="$(pwd -P)"
mkdir -p "$repo_root/tmp"
build_dir="$(mktemp -d "$repo_root/tmp/live-wheel.XXXXXX")"
CODEX_CHATS_BUILD_DEBUG=0 python3.13 -m pip wheel --no-deps --wheel-dir "$build_dir" "$repo_root"
wheel_path="$(print -r -- "$build_dir"/*.whl)"
test -f "$wheel_path"
codex_install_home="${CODEX_HOME:-$HOME/.codex}"
install_dir="${CODEX_CHATS_INSTALL_DIR:-$codex_install_home/mcp/codex-chats}"
mkdir -p "${install_dir:h}"
install_dir="$(cd -P "${install_dir:h}" && pwd -P)/${install_dir:t}"
print -r -- "rebuild target: $install_dir"
if [[ "$install_dir" != /*/codex-chats || -L "$install_dir" ]]; then
  print -u2 -- "refusing target: $install_dir"
  exit 1
fi
if [[ -e "$install_dir" ]]; then
  backup_dir="$(mktemp -d "${install_dir}.backup.XXXXXX")"
  mv -- "$install_dir" "$backup_dir/venv"
  print -r -- "old installation saved at: $backup_dir/venv"
fi
python3.13 -m venv "$install_dir"
"$install_dir/bin/python" -m pip install 'mcp[cli]==2.1.1'
"$install_dir/bin/python" -m pip install --no-deps "$wheel_path"
"$install_dir/bin/python" -m pip check
print -r -- "Codex command: $install_dir/bin/codex-chats-mcp"
```

Keep the backup until the new installation passes its smoke test. To roll back, stop the connector, move the failed new directory aside, and move the saved `venv` back to the original install path; venv executables should not be run directly from the backup location.

## 6. Configure Codex

Use the absolute command path printed by the rebuild. Release builds ignore the runtime logging opt-in. For local debugging, build and install a separate debug wheel from source instead of the release wheel in section 5:

```zsh
set -euo pipefail
repo_root="$(pwd -P)"
mkdir -p "$repo_root/tmp"
build_dir="$(mktemp -d "$repo_root/tmp/debug-wheel.XXXXXX")"
codex_install_home="${CODEX_HOME:-$HOME/.codex}"
install_dir="${CODEX_CHATS_INSTALL_DIR:-$codex_install_home/mcp/codex-chats}"
test -x "$install_dir/bin/python"
CODEX_CHATS_BUILD_DEBUG=1 python3.13 -m pip wheel --no-deps --wheel-dir "$build_dir/debug" "$repo_root"
"$install_dir/bin/python" -m pip install --no-deps --force-reinstall "$build_dir/debug/"*.whl
```

The build flag must be exactly `0` (default release) or `1` (debug). Debug capability is stamped into the wheel, never into the source checkout. Both modes keep the same package version, so use separate output directories and force reinstall when switching modes. Debug builds require a wheel install, not `pip install -e .`; source changes need a rebuild. Private debug wheels must not be uploaded to PyPI.

For that debug build only, add the runtime logging environment table:

```toml
[mcp_servers.codex-chats]
command = "/absolute/path/to/codex-chats/bin/codex-chats-mcp"

[mcp_servers.codex-chats.env]
CODEX_CHATS_DEBUG_LOG = "1"
```

Keep `CODEX_CHATS_DEBUG_LOG` unset or `0` for normal use. A release build cannot enable file logging even if an old configuration still says `1`. In a debug build, `CODEX_CHATS_ERROR_LOG` is optional and must point to a trusted private regular file; `off` disables file logging. Choose any tool allowlist your own client supports; this guide does not prescribe one.

## 7. Restart, smoke-test, and inspect the log

```zsh
set -euo pipefail
codex mcp get codex-chats
```

Restart or reload Codex after changing the server: active sessions can retain an old process and tool schema. Run only the read-only smoke tools `list_conversations`, `search_conversations`, and `get_conversation`. In an opted-in debug build, inspect only the sanitized `<install_dir>/codex-chats-mcp-errors.log` (or your trusted `CODEX_CHATS_ERROR_LOG` path). Release builds never create this log; in debug builds, a missing log is also expected when calls succeed because success calls intentionally add nothing. Error records use a locally generated `server_instance_id`, a keyed digest rather than a raw peer request ID, and bounded tool-call correlation; stdio does not expose a real Codex conversation/session ID.

Debug logging requires both a debug wheel and runtime opt-in. It has a hard 8 MiB cap, trims old complete records to about 6 MiB, and uses a cross-process lock while appending/trimming, so concurrent Codex sessions do not interleave unsafe writes. If locking is unavailable it fails closed. Logs contain bounded error metadata and redacted resource IDs only—never successes, message content, authentication, request/response bodies, or queries.

## 8. CI and the future PyPI release

The `Test` workflow runs on pushes to `develop` and `main`, and on pull requests targeting either branch, including documentation-only changes. Its matrix covers Linux with Python 3.10, 3.13, and 3.14, plus Windows and macOS with Python 3.13. Hosted macOS CI does not specifically test Big Sur.

Each matrix entry builds a release-mode sdist and wheel, checks the package/README with `twine check --strict`, installs the wheel with exact MCP 2.1.1, checks dependencies and installed-module paths, compiles the code, and runs the full suite from an isolated directory. It also builds a separate debug wheel and checks both runtime opt-in states. Tests use fake authentication and a handshake/tool-list check, not live conversation requests. The live venv command above uses POSIX paths; Windows uses the venv's `Scripts` directory.

After all matrix entries pass, `Test` calls `Build packages` (`build.yml`) at the same commit. That workflow creates and verifies the final release-mode wheel and source archive, tests the final wheel, and retains them as the workflow's downloadable `dist` artifact for 14 days. Neither workflow creates a GitHub Release or uploads to PyPI; `develop` only receives tests and artifacts. Debug smoke-test wheels are never included in `dist`.

The separate `Publish to PyPI` workflow is dormant and may be used only for a manual `workflow_dispatch` from the `main` ref. Its owner gate must match `XxUnkn0wnxX/codex-chats-mcp`, the repository variable `ENABLE_PYPI_PUBLISH` must be exactly `true`, `confirm_publish` must be `true`, `expected_name` must be `codex-chats-mcp-v2`, and `expected_version` must match the package version (`0.2.0` for the first release). Keep the gate unset/disabled until the release prerequisites are complete.

Every publish run calls the complete `Test` → `Build packages` pipeline at the exact `main` commit being released. All matrix entries and the final build must succeed; failed, cancelled, or skipped tests cannot unlock publishing. Release validation checks the confirmed stable version, fork package name, README metadata, and disabled debug stamps in both archives. Only then does the separate publish job upload the same run's retained artifacts, without rebuilding or taking files from another branch, through PyPI Trusted Publishing (OIDC). It does not use a long-lived PyPI token.

Publishing is manual, not change-detected: source edits, README edits, version bumps, tags, and ordinary pushes do not trigger an upload. Choose a new stable version in `pyproject.toml` for each release, keep the User-Agent version in sync, and supply that version when dispatching from tested `main`. An unchanged version does not create a new release, and PyPI will reject attempts to replace already-uploaded distribution filenames.

The PyPI headline comes from `project.description` in `pyproject.toml`. The full project description comes from `readme = "README.md"`, captured when the package is built. This is the same metadata mechanism used by upstream. Updating the GitHub README alone does not update an existing PyPI release's description; it is included with the next published version.

Before the first fork release, create one pending PyPI Trusted Publisher registration for project `codex-chats-mcp-v2` using the [trusted-publisher project creation guide](https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/). Set owner `XxUnkn0wnxX`, repository `codex-chats-mcp`, workflow `publish.yml`, and environment `pypi`; this pending registration creates the project on its first upload. See the [Trusted Publisher usage guide](https://docs.pypi.org/trusted-publishers/using-a-publisher/) for the registration details. Configure the GitHub `pypi` environment to restrict deployment to `main` and require a reviewer if available. Keep these setup steps as prerequisites before enabling the gate.
