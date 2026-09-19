"""Keep the publishing entry points attached to the complete release gates."""

import unittest
from pathlib import Path

import yaml


WORKFLOWS = Path(__file__).resolve().parents[1] / "workflows"


def workflow(name):
    # BaseLoader preserves GitHub's `on` key and scalar strings verbatim.
    return yaml.load((WORKFLOWS / name).read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


def expression(text):
    return " ".join(text.split())


class ReleaseWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.publish = workflow("publish.yml")
        self.test = workflow("test.yml")
        self.build = workflow("build.yml")

    def test_only_main_push_and_confirmed_manual_bootstrap_entry_points(self):
        events = self.publish["on"]
        self.assertEqual(set(events), {"push", "workflow_dispatch"})
        self.assertEqual(events["push"], {"branches": ["main"]})
        inputs = events["workflow_dispatch"]["inputs"]
        self.assertEqual(set(inputs), {"confirm_publish", "expected_version"})
        self.assertEqual(inputs["confirm_publish"]["default"], "false")
        self.assertEqual(inputs["confirm_publish"]["type"], "boolean")
        self.assertEqual(inputs["expected_version"]["required"], "true")

    def test_gate_uses_exact_commit_full_history_and_read_only_permissions(self):
        gate = self.publish["jobs"]["version"]
        self.assertEqual(
            expression(gate["if"]),
            "${{ github.repository == 'XxUnkn0wnxX/codex-chats-mcp' && "
            "github.ref == 'refs/heads/main' && "
            "(github.event_name == 'push' || "
            "(github.event_name == 'workflow_dispatch' && inputs.confirm_publish == true)) }}",
        )
        checkout = gate["steps"][0]
        self.assertTrue(checkout["uses"].startswith("actions/checkout@"))
        self.assertEqual(checkout["with"], {
            "ref": "${{ github.sha }}", "fetch-depth": "0", "persist-credentials": "false",
        })
        runner = next(step for step in gate["steps"] if step.get("id") == "gate")
        self.assertEqual(runner["run"], "python .github/scripts/release_gate.py")
        self.assertEqual(gate["outputs"], {
            "should_publish": "${{ steps.gate.outputs.should_publish }}",
            "version": "${{ steps.gate.outputs.version }}",
        })
        self.assertEqual(self.publish["permissions"], {"contents": "read"})
        self.assertNotIn("permissions", gate)

    def test_exact_version_requires_full_same_commit_pipeline(self):
        build = self.publish["jobs"]["build"]
        self.assertEqual(build["needs"], "version")
        self.assertEqual(build["uses"], "./.github/workflows/test.yml")
        self.assertEqual(build["with"], {
            "expected_name": "codex-chats-mcp-v2",
            "expected_version": "${{ needs.version.outputs.version }}",
            "release": "true",
        })
        self.assertEqual(
            expression(build["if"]),
            "${{ github.repository == 'XxUnkn0wnxX/codex-chats-mcp' && "
            "github.ref == 'refs/heads/main' && needs.version.result == 'success' && "
            "needs.version.outputs.should_publish == 'true' }}",
        )
        downstream = self.test["jobs"]["build"]
        self.assertEqual(downstream["needs"], "test")
        self.assertEqual(downstream["if"], "${{ needs.test.result == 'success' }}")
        self.assertEqual(downstream["uses"], "./.github/workflows/build.yml")
        self.assertEqual(downstream["with"], {
            "expected_name": "${{ inputs.expected_name || 'codex-chats-mcp-v2' }}",
            "expected_version": "${{ inputs.expected_version || '' }}",
            "release": "${{ inputs.release || false }}",
        })

    def test_upload_has_no_bypass_or_cross_run_artifacts(self):
        publish = self.publish["jobs"]["publish"]
        self.assertEqual(publish["needs"], ["version", "build"])
        self.assertEqual(
            expression(publish["if"]),
            "${{ github.repository == 'XxUnkn0wnxX/codex-chats-mcp' && "
            "github.ref == 'refs/heads/main' && needs.version.result == 'success' && "
            "needs.version.outputs.should_publish == 'true' && needs.build.result == 'success' }}",
        )
        self.assertEqual(publish["environment"]["name"], "pypi")
        self.assertEqual(publish["permissions"], {"actions": "read", "id-token": "write"})
        download, upload = publish["steps"]
        self.assertTrue(download["uses"].startswith("actions/download-artifact@"))
        self.assertEqual(download["with"], {"name": "dist", "path": "dist/"})
        self.assertTrue(upload["uses"].startswith("pypa/gh-action-pypi-publish@"))
        self.assertNotIn("with", upload)  # No skip-existing or password/token shortcuts.
        self.assertEqual(self.publish["concurrency"], {
            "group": "pypi-publish", "cancel-in-progress": "false",
        })

    def test_all_platforms_debug_off_and_release_validation_remain_required(self):
        self.assertEqual(self.test["on"]["push"]["branches"], ["develop", "main"])
        matrix = self.test["jobs"]["test"]["strategy"]["matrix"]["include"]
        self.assertEqual({(item["os"], item["python-version"]) for item in matrix}, {
            ("ubuntu-latest", "3.10"), ("ubuntu-latest", "3.13"),
            ("ubuntu-latest", "3.14"), ("windows-latest", "3.13"), ("macos-latest", "3.13"),
        })
        self.assertEqual(self.test["jobs"]["test"]["strategy"]["fail-fast"], "false")
        for env in (self.test["env"], self.build["jobs"]["build"]["env"]):
            self.assertEqual(env["CODEX_CHATS_BUILD_DEBUG"], "0")
            self.assertEqual(env["CODEX_CHATS_DEBUG_LOG"], "0")
        build = self.build["jobs"]["build"]
        self.assertEqual(build["if"], "${{ !inputs.release || github.ref == 'refs/heads/main' }}")
        steps = build["steps"]
        commands = "\n".join(step.get("run", "") for step in steps)
        self.assertIn("python -m unittest discover -s .github/scripts", commands)
        self.assertIn("release_args+=(--release)", commands)
        self.assertIn('sha256sum --check "$RUNNER_TEMP/codex-chats-release.sha256"', commands)
        self.assertTrue(steps[-1]["uses"].startswith("actions/upload-artifact@"))
        self.assertEqual(steps[-1]["with"]["path"], "dist/")

    def test_no_gate_can_ignore_a_test_or_build_failure(self):
        for document in (self.publish, self.test, self.build):
            for job in document["jobs"].values():
                self.assertNotIn("continue-on-error", job)
                for step in job.get("steps", []):
                    self.assertNotIn("continue-on-error", step)


if __name__ == "__main__":
    unittest.main()
