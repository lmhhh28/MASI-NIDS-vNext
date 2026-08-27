from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError
from referencing import Registry, Resource

from masi_analysis.canonical import compute_artifact_digest, compute_input_digest, file_digest
from masi_analysis.models import AnalysisArtifact, FrozenInput, ProviderRequest, ProviderResponse


class ContractGoldenTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.contracts = Path(__file__).resolve().parents[2] / "contracts"
        resources: list[tuple[str, Resource[Any]]] = []
        cls.schemas: dict[str, dict[str, Any]] = {}
        for path in cls.contracts.rglob("*.json"):
            try:
                value = json.loads(path.read_bytes())
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if isinstance(value, dict) and value.get("$schema") == "https://json-schema.org/draft/2020-12/schema" and isinstance(value.get("$id"), str):
                Draft202012Validator.check_schema(value)
                cls.schemas[str(path.relative_to(cls.contracts))] = value
                resources.append((value["$id"], Resource.from_contents(value)))
        cls.registry = Registry().with_resources(resources)
        cls.golden = cls.contracts / "analysis/v1/golden"

    @classmethod
    def validate(cls, schema_path: str, value: dict[str, Any]) -> None:
        validator = Draft202012Validator(
            cls.schemas[schema_path],
            registry=cls.registry,
            format_checker=FormatChecker(),
        )
        validator.validate(value)

    def load(self, name: str) -> dict[str, Any]:
        value = json.loads((self.golden / name).read_bytes())
        self.assertIsInstance(value, dict)
        return value

    def test_shared_input_artifact_and_wire_goldens(self) -> None:
        input_value = self.load("input-v1.json")
        artifact_value = self.load("artifact-v1.json")
        send_value = self.load("a2a-send-request-v1.json")
        completed_value = self.load("a2a-task-completed-v1.json")
        self.validate("analysis/input/v1/schema.json", input_value)
        self.validate("analysis/artifact/v1/schema.json", artifact_value)
        self.validate("analysis/a2a/v1/send-request.schema.json", send_value)
        self.validate("analysis/a2a/v1/task-response.schema.json", completed_value)
        self.validate(
            "analysis/a2a/v1/peer-task-response.schema.json",
            self.load("a2a-peer-task-completed-v1.json"),
        )
        input_bundle = FrozenInput.model_validate(input_value)
        artifact = AnalysisArtifact.model_validate(artifact_value)
        self.assertEqual(input_bundle.input_digest, compute_input_digest(input_bundle))
        self.assertEqual(artifact.artifact_digest, compute_artifact_digest(artifact))

    def test_provider_agent_card_and_error_goldens(self) -> None:
        request_value = self.load("provider-request-v1.json")
        response_value = self.load("provider-response-v1.json")
        self.validate("analysis/provider/v1/schema.json", request_value)
        self.validate("analysis/provider/v1/schema.json", response_value)
        ProviderRequest.model_validate(request_value)
        ProviderResponse.model_validate(response_value)
        self.validate("analysis/a2a/v1/agent-card.schema.json", self.load("a2a-agent-card-v1.json"))
        self.validate("analysis/a2a/v1/error.schema.json", self.load("a2a-error-v1.json"))

    def test_catalog_digests_and_negative_vectors(self) -> None:
        catalog = self.load("catalog.json")
        for key, item in catalog.items():
            if key == "schema_version":
                continue
            self.assertIsInstance(item, dict)
            raw = (self.golden / item["path"]).read_bytes().rstrip(b"\n")
            if "file_digest" in item:
                self.assertEqual(item["file_digest"], file_digest(raw))
            elif "content_digest" in item:
                self.assertEqual(item["content_digest"], file_digest(raw))
        bad_input = copy.deepcopy(self.load("input-v1.json"))
        bad_input["schema_version"] = "masi-analysis-input/v2"
        with self.assertRaises(ValidationError):
            self.validate("analysis/input/v1/schema.json", bad_input)
        executable = copy.deepcopy(self.load("artifact-v1.json"))
        executable["non_executable"] = False
        executable["deployment_eligible"] = True
        with self.assertRaises(ValidationError):
            self.validate("analysis/artifact/v1/schema.json", executable)


if __name__ == "__main__":
    unittest.main()
