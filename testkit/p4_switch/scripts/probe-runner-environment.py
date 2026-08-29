from __future__ import annotations

import argparse
import importlib.metadata
import json
import platform
import re
import subprocess
from pathlib import Path
from typing import Any


def load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} is not a JSON object")
    return value


def run(arguments: list[str]) -> str:
    completed = subprocess.run(
        arguments,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise RuntimeError(
            f"{' '.join(arguments)} exited {completed.returncode}: {detail[:512]}"
        )
    return completed.stdout.strip() or completed.stderr.strip()


def os_release(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value.strip().strip('"')
    return values


def match_version(pattern: str, value: str, name: str) -> str:
    matched = re.search(pattern, value, flags=re.MULTILINE)
    if matched is None:
        raise ValueError(f"cannot parse {name} from {value[:512]!r}")
    return matched.group(1)


def package_versions(value: str) -> dict[str, str]:
    versions: dict[str, str] = {}
    for name in ("iproute2", "tcpreplay"):
        versions[name] = match_version(
            rf"^{re.escape(name)}-(\S+)\s+x86_64\s+",
            value,
            f"{name} Alpine package version",
        )
    return versions


def observe() -> tuple[dict[str, object], dict[str, str]]:
    release = os_release(Path("/etc/os-release"))
    alpine_release = Path("/etc/alpine-release").read_text(encoding="utf-8").strip()
    ip_output = run(["ip", "-V"])
    tcpreplay_output = run(["tcpreplay", "--version"])
    apk_output = run(["apk", "list", "--installed", "iproute2", "tcpreplay"])
    packages = package_versions(apk_output)
    observed: dict[str, object] = {
        "distribution": release.get("NAME", ""),
        "distribution_version": alpine_release,
        "architecture": platform.machine(),
        "python": platform.python_version(),
        "p4runtime": importlib.metadata.version("p4runtime"),
        "grpcio": importlib.metadata.version("grpcio"),
        "ptf": importlib.metadata.version("ptf"),
        "scapy": importlib.metadata.version("scapy"),
        "iproute2": {
            "tool_version": match_version(
                r"iproute2-v([^\s,]+)", ip_output, "iproute2 tool version"
            ),
            "package_version": packages["iproute2"],
        },
        "tcpreplay": {
            "tool_version": match_version(
                r"tcpreplay version:\s*([^\s]+)",
                tcpreplay_output,
                "tcpreplay tool version",
            ),
            "package_version": packages["tcpreplay"],
        },
    }
    raw = {
        "alpine_release": alpine_release,
        "os_release_name": release.get("NAME", ""),
        "ip_version": ip_output[:512],
        "tcpreplay_version": tcpreplay_output[:512],
        "apk_list": apk_output[:1024],
    }
    return observed, raw


def phase(
    runner_digest: str,
    expected: dict[str, object] | None,
    observed: dict[str, object] | None,
    raw: dict[str, str],
    error: str | None,
) -> dict[str, object]:
    exact_match = error is None and expected is not None and observed == expected
    result = "PASS" if exact_match else "FAIL"
    evidence: dict[str, object] = {
        "runner_image_digest": runner_digest,
        "expected": expected or {},
        "observed": observed or {},
        "exact_match": exact_match,
        "raw": raw,
    }
    if error is not None:
        evidence["error"] = error[:1024]
    return {
        "phase": "runner-environment",
        "level": "MODULE",
        "applicability": "APPLICABLE",
        "result": result,
        "qualification": "QUALIFIED" if result == "PASS" else "NOT_QUALIFIED",
        "tests": [
            {
                "id": "TEST-TRAFFIC-001-runner-runtime-version",
                "requirement_ids": [
                    "CONTRACT-TRAFFIC-001",
                    "TEST-TRAFFIC-001",
                    "TEST-003",
                ],
                "level": "MODULE",
                "applicability": "APPLICABLE",
                "result": result,
                "qualification": ("QUALIFIED" if result == "PASS" else "NOT_QUALIFIED"),
                "evidence": evidence,
            }
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--runner-digest", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    expected: dict[str, object] | None = None
    observed: dict[str, object] | None = None
    raw: dict[str, str] = {}
    error: str | None = None
    try:
        profile = load(args.repo / "contracts/profiles/v1/e2e-runner-compose.json")
        profile_environment = profile["runner_environment"]
        if not isinstance(profile_environment, dict):
            raise ValueError("runner_environment is not an object")
        expected = profile_environment
        observed, raw = observe()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        error = f"{type(exc).__name__}: {exc}"

    document = phase(args.runner_digest, expected, observed, raw, error)
    args.output.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return 0 if document["result"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
