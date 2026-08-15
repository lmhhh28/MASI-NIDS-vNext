#!/usr/bin/env python3
"""Deterministic negative cases for the bounded OCI archive inspector."""

from __future__ import annotations

import argparse
import copy
import io
import json
import runpy
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from typing import Callable


Mutation = Callable[[tarfile.TarInfo, bytes], tuple[tarfile.TarInfo, bytes] | None]


def invoke(repo: Path, archive: Path, image_ref: str, output: Path) -> int:
    return subprocess.run(
        [
            sys.executable,
            str(repo / "edge-rs/scripts/inspect-oci-archive.py"),
            "--archive",
            str(archive),
            "--image-ref",
            image_ref,
            "--index-output",
            str(output / "image-archive-manifest.json"),
            "--config-output",
            str(output / "image-config.json"),
            "--summary-output",
            str(output / "oci-archive-inspection.json"),
        ],
        check=False,
        capture_output=True,
        text=True,
    ).returncode


def rewrite(source: Path, destination: Path, mutation: Mutation) -> None:
    with (
        tarfile.open(source, "r:") as existing,
        tarfile.open(destination, "w:") as output,
    ):
        for member in existing:
            payload = b""
            if member.isfile():
                stream = existing.extractfile(member)
                if stream is None:
                    raise ValueError(f"unreadable source member {member.name}")
                payload = stream.read()
            transformed = mutation(copy.copy(member), payload)
            if transformed is None:
                continue
            transformed_member, transformed_payload = transformed
            transformed_member.size = (
                len(transformed_payload) if transformed_member.isfile() else 0
            )
            output.addfile(
                transformed_member,
                io.BytesIO(transformed_payload)
                if transformed_member.isfile()
                else None,
            )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--image-ref", required=True)
    args = parser.parse_args()
    repo = args.repo.resolve()
    archive = args.archive.resolve()

    inspector = runpy.run_path(
        str(repo / "edge-rs/scripts/inspect-oci-archive.py"),
        run_name="oci_archive_inspector_library",
    )
    register_unique_layer_member = inspector["register_unique_layer_member"]
    observed_layer_paths: set[str] = set()
    register_unique_layer_member(observed_layer_paths, "usr/local/bin/masi-edge")
    try:
        register_unique_layer_member(observed_layer_paths, "usr/local/bin/masi-edge")
    except ValueError:
        duplicate_layer_path_rejected = True
    else:
        duplicate_layer_path_rejected = False
    if not duplicate_layer_path_rejected:
        raise SystemExit("OCI inspector accepted a duplicate layer filesystem path")

    with tempfile.TemporaryDirectory(prefix="masi-edge-oci-negative-") as directory:
        root = Path(directory)
        if invoke(repo, archive, args.image_ref, root / "valid") != 0:
            raise SystemExit("OCI inspector rejected the unmodified archive")

        with tarfile.open(archive, "r:") as source:
            manifest_stream = source.extractfile("manifest.json")
            if manifest_stream is None:
                raise SystemExit("source OCI archive has no manifest.json")
            manifest = json.load(manifest_stream)
        config_path = str(manifest[0]["Config"])
        first_layer = str(manifest[0]["Layers"][0])

        mutations: dict[str, Mutation] = {
            "config-content-digest": lambda member, payload: (
                member,
                (payload[:-1] + bytes([payload[-1] ^ 1]))
                if member.name == config_path and payload
                else payload,
            ),
            "missing-layer": lambda member, payload: (
                None if member.name == first_layer else (member, payload)
            ),
        }
        for name, mutation in mutations.items():
            candidate = root / f"{name}.tar"
            rewrite(archive, candidate, mutation)
            if invoke(repo, candidate, args.image_ref, root / f"output-{name}") == 0:
                raise SystemExit(f"OCI inspector accepted forbidden mutation: {name}")

        traversal = root / "path-traversal.tar"
        rewrite(archive, traversal, lambda member, payload: (member, payload))
        with tarfile.open(traversal, "a:") as output:
            member = tarfile.TarInfo("../escape")
            member.size = 1
            output.addfile(member, io.BytesIO(b"x"))
        if invoke(repo, traversal, args.image_ref, root / "output-traversal") == 0:
            raise SystemExit("OCI inspector accepted a path-traversal member")

        symbolic = root / "symbolic-member.tar"
        rewrite(archive, symbolic, lambda member, payload: (member, payload))
        with tarfile.open(symbolic, "a:") as output:
            member = tarfile.TarInfo("forbidden-link")
            member.type = tarfile.SYMTYPE
            member.linkname = "manifest.json"
            output.addfile(member)
        if invoke(repo, symbolic, args.image_ref, root / "output-symbolic") == 0:
            raise SystemExit("OCI inspector accepted a symbolic archive member")

        symlink_output = root / "symlink-output"
        symlink_output.mkdir()
        target = symlink_output / "target.json"
        target.write_text("{}", encoding="utf-8")
        (symlink_output / "oci-archive-inspection.json").symlink_to(target.name)
        if invoke(repo, archive, args.image_ref, symlink_output) == 0:
            raise SystemExit("OCI inspector replaced a symbolic output")

    print(
        json.dumps(
            {
                "schema_version": "edge-oci-archive-inspector-negatives/v1",
                "valid_archive": "accepted",
                "forbidden_mutations": [
                    "config-content-digest",
                    "missing-layer",
                    "path-traversal",
                    "symbolic-member",
                    "symbolic-output",
                    "duplicate-layer-path",
                ],
                "result": "PASS",
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
