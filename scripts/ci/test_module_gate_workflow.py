from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class ModuleGateWorkflowTests(unittest.TestCase):
    def test_preflight_failure_is_uploaded_but_still_fails_adjudication(self) -> None:
        workflow = (ROOT / ".github/workflows/module-gates.yml").read_text(encoding="utf-8")
        self.assertIn(
            'echo "exit_code=${{ steps.preflight.outputs.exit_code }}" >>"${GITHUB_OUTPUT}"',
            workflow,
        )
        self.assertIn('if [[ "${GATE_EXIT_CODE}" -ne 0 ]]; then', workflow)
        self.assertIn('exit "${GATE_EXIT_CODE}"', workflow)
        self.assertLess(
            workflow.index('python3 scripts/ci/verify_module_gate_output.py'),
            workflow.index('exit "${GATE_EXIT_CODE}"'),
        )


if __name__ == "__main__":
    unittest.main()
