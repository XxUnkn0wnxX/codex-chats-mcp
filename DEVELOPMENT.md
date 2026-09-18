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

Keep development, review, and pushes on `develop`; review upstream changes before selectively integrating them. Put private workstation-only paths in the clone-local `.git/info/exclude`. Standard project artifacts such as `.venv/` remain in the shared `.gitignore`:

```zsh
set -euo pipefail
git rev-parse --git-path info/exclude
git check-ignore -v tmp .venv
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

## 4. Test a clean wheel

This fail-fast subshell captures `repo_root` before changing directory, builds into project-local ignored `tmp/`, tests from an empty cwd, and propagates failures. The metadata check proves the wheel, not the checkout, is imported:

```zsh
(
  set -euo pipefail
  repo_root="$(pwd -P)"
  mkdir -p "$repo_root/tmp"
  build_dir="$(mktemp -d "$repo_root/tmp/clean-wheel.XXXXXX")"
  python3.13 -m pip wheel --no-deps --wheel-dir "$build_dir" "$repo_root"
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
for name, module in (("codex_chats_mcp", codex_chats_mcp), ("mcp", mcp)):
    module_path = Path(module.__file__).resolve()
    assert "site-packages" in module_path.parts, module_path
    print(f"{name}: {module_path}")
PY
  )
)
```

## 5. Rebuild the live venv

Build the wheel before changing the live installation. `CODEX_HOME` defaults to `$HOME/.codex`; `CODEX_CHATS_INSTALL_DIR` can override the install directory. The target itself is the venv, so do not create a nested `.venv`.

```zsh
set -euo pipefail
repo_root="$(pwd -P)"
mkdir -p "$repo_root/tmp"
build_dir="$(mktemp -d "$repo_root/tmp/live-wheel.XXXXXX")"
python3.13 -m pip wheel --no-deps --wheel-dir "$build_dir" "$repo_root"
wheel_path="$(print -r -- "$build_dir"/*.whl)"
test -f "$wheel_path"
codex_home="${CODEX_HOME:-$HOME/.codex}"
install_dir="${CODEX_CHATS_INSTALL_DIR:-$codex_home/mcp/codex-chats}"
mkdir -p "${install_dir:h}"
install_dir="$(cd -P "${install_dir:h}" && pwd -P)/${install_dir:t}"
print -r -- "rebuild target: $install_dir"
if [[ "$install_dir" != /*/codex-chats ]]; then
  print -u2 -- "refusing target: $install_dir"
  exit 1
fi
rm -rf -- "$install_dir"
python3.13 -m venv "$install_dir"
"$install_dir/bin/python" -m pip install 'mcp[cli]==2.1.1'
"$install_dir/bin/python" -m pip install --no-deps "$wheel_path"
"$install_dir/bin/python" -m pip check
print -r -- "Codex command: $install_dir/bin/codex-chats-mcp"
```

## 6. Configure Codex

Use the absolute command path printed by the rebuild. For a test build, add the opt-in debug environment table:

```toml
[mcp_servers.codex-chats]
command = "/absolute/path/to/codex-chats/bin/codex-chats-mcp"

[mcp_servers.codex-chats.env]
CODEX_CHATS_DEBUG_LOG = "1"
```

Remove the `env` table, or at least the debug flag, for clean release/main behavior. `CODEX_CHATS_ERROR_LOG` is optional and must point to a trusted private regular file; `off` disables file logging. Choose any tool allowlist your own client supports; this guide does not prescribe one.

## 7. Restart, smoke-test, and inspect the log

```zsh
set -euo pipefail
codex mcp get codex-chats
```

Restart or reload Codex after changing the server: active sessions can retain an old process and tool schema. Run only the read-only smoke tools `list_conversations`, `search_conversations`, and `get_conversation`. Inspect only the sanitized `<install_dir>/codex-chats-mcp-errors.log` (or your trusted `CODEX_CHATS_ERROR_LOG` path). A missing log is expected when calls succeed; success calls intentionally add nothing. Error records use a locally generated `server_instance_id`, a keyed digest rather than a raw peer request ID, and bounded tool-call correlation; stdio does not expose a real Codex conversation/session ID.

Debug logging is opt-in. It has a hard 8 MiB cap, trims old complete records to about 6 MiB, and uses a cross-process lock while appending/trimming, so concurrent Codex sessions do not interleave unsafe writes. If locking is unavailable it fails closed. Logs contain bounded error metadata and redacted resource IDs only—never successes, message content, authentication, request/response bodies, or queries.

## 8. CI and the future PyPI release

Keep the Linux and Windows CI matrix enabled. It builds and installs a wheel with exact MCP 2.1.1, checks dependencies, and runs the full suite from an isolated directory. The live venv command above uses POSIX paths; Windows uses the venv's `Scripts` directory.

The normal `Test` workflow builds and tests only; it never publishes and needs no PyPI token.

The separate `Publish to PyPI` workflow is dormant and may be used only for a manual `workflow_dispatch` from the `main` ref. Its owner gate must match `XxUnkn0wnxX/codex-chats-mcp`, the repository variable `ENABLE_PYPI_PUBLISH` must be exactly `true`, `confirm_publish` must be `true`, `expected_name` must be `codex-chats-mcp-v2`, and `expected_version` must match the package version (`0.2.0` for the first release). Keep the gate unset/disabled until the release prerequisites are complete.

The build job creates an sdist and wheel, runs `twine check`, and tests the installed wheel offline in an isolated environment. Only after those checks pass does the separate publish job upload the retained artifacts through PyPI Trusted Publishing (OIDC); it does not use a long-lived PyPI token.

Before the first fork release, create one pending PyPI Trusted Publisher registration for project `codex-chats-mcp-v2` using the [trusted-publisher project creation guide](https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/). Set owner `XxUnkn0wnxX`, repository `codex-chats-mcp`, workflow `publish.yml`, and environment `pypi`; this pending registration creates the project on its first upload. See the [Trusted Publisher usage guide](https://docs.pypi.org/trusted-publishers/using-a-publisher/) for the registration details. Configure the GitHub `pypi` environment to restrict deployment to `main` and require a reviewer if available. Keep these setup steps as prerequisites before enabling the gate.
