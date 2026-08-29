from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
GENERATED = ROOT / "contracts/generated/typescript/control-api"
OPENAPI = ROOT / "contracts/openapi/v1/openapi.yaml"


class GeneratedClientGuardTests(unittest.TestCase):
    def test_http_errors_throw_by_default(self) -> None:
        runtime = (GENERATED / "client/utils.gen.ts").read_text(encoding="utf-8")
        types = (GENERATED / "client/types.gen.ts").read_text(encoding="utf-8")
        self.assertEqual(1, runtime.count("throwOnError: true"))
        self.assertEqual(1, runtime.count("responseStyle: 'fields'"))
        self.assertIn("Throw an error instead of returning it in the response?", types)
        self.assertEqual(1, types.count("* @default true\n   */\n  throwOnError?"))
        self.assertNotIn("* @default false\n   */\n  throwOnError?", types)

    def test_sse_retry_count_has_a_finite_default(self) -> None:
        source = (GENERATED / "core/serverSentEvents.gen.ts").read_text(encoding="utf-8")
        self.assertIn("attempt >= (sseMaxRetryAttempts ?? 8)", source)
        self.assertNotIn("sseMaxRetryAttempts !== undefined &&", source)
        self.assertIn("throw new Error('SSE stream ended')", source)
        self.assertIn("attempt = 0;", source)
        self.assertIn("if (signal.aborted) break;", source)
        self.assertLess(source.index("attempt = 0;"), source.index("throw new Error('SSE stream ended')"))

    def test_mutation_digest_inputs_reuse_the_nonzero_digest_schema(self) -> None:
        source = OPENAPI.read_text(encoding="utf-8")
        for field in ("revision_digest", "effect_digest", "manifest_digest", "wit_digest", "service_proto_digest"):
            self.assertIn(f"{field}: {{$ref: '#/components/schemas/Digest'}}", source)


if __name__ == "__main__":
    unittest.main()
