#!/usr/bin/env python3
"""Materialize the digest-pinned, read-only Triton model repository closure.

One artifact serves two consumers so their digests can never drift:
  - the Triton container mounts this directory read-only;
  - the Gateway verifies it as the `model_repository_path` closure.

Layout (Triton `model-control-mode=none` exact-binding closure):

  <out>/closure-manifest.json            closure member set (not a member itself)
  <out>/bundle-manifest.json             role=bundle-manifest (adapter/taxonomy data)
  <out>/<model>/config.pbtxt             role=config
  <out>/<model>/1/model.onnx             role=model

closure_digest preimage binds role and path, not only content:

  sorted(f"{role}\t{rel_path}\t{member_digest}") joined by "\n" with a trailing "\n"

so relocating or re-roling a member changes the digest. Digest of that preimage
is `sha256:<hex>`.

Usage:
  ./build_repository.py --revision r2 --out testkit/fixtures/repositories/masi-ids-window-v1-r2
"""
import argparse
import hashlib
import json
import os
import shutil
import stat

MODEL_ID = "masi-ids-window-v1"

CONFIG_PBTXT_R2 = """name: "masi-ids-window-v1"
backend: "onnxruntime"
max_batch_size: 256
input [
  {
    name: "features"
    data_type: TYPE_UINT64
    dims: [ 6 ]
  }
]
output [
  {
    name: "scores"
    data_type: TYPE_FP32
    dims: [ 2 ]
  }
]
dynamic_batching {
  preferred_batch_size: [ 32, 64, 128, 256 ]
  max_queue_delay_microseconds: 200
  default_queue_policy {
    max_queue_size: 1024
  }
}
instance_group [
  {
    count: 1
    kind: KIND_CPU
  }
]
"""


def sha256_bytes(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return f"sha256:{h.hexdigest()}"


def closure_digest(members: list[dict]) -> str:
    lines = sorted(
        f"{m['role']}\t{m['rel_path']}\t{m['member_digest']}" for m in members
    )
    return sha256_bytes(("\n".join(lines) + "\n").encode("utf-8"))


def make_readonly(root: str) -> None:
    """Read-only tree: 0444 files, 0555 dirs (closure verification requires it)."""
    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        for name in filenames:
            os.chmod(os.path.join(dirpath, name), stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
        for name in dirnames:
            os.chmod(
                os.path.join(dirpath, name),
                stat.S_IRUSR | stat.S_IXUSR | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH,
            )
    os.chmod(
        root,
        stat.S_IRUSR | stat.S_IXUSR | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH,
    )


def make_writable(root: str) -> None:
    for dirpath, dirnames, filenames in os.walk(root):
        os.chmod(dirpath, 0o755)
        for name in filenames:
            os.chmod(os.path.join(dirpath, name), 0o644)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--revision", default="r2", choices=["r2"])
    parser.add_argument("--out", required=True, help="Repository output directory")
    parser.add_argument(
        "--fixtures-dir",
        default=None,
        help="Directory holding the generated fixture artifacts (default: ../ of this script)",
    )
    args = parser.parse_args()

    here = os.path.dirname(os.path.abspath(__file__))
    fixtures_dir = args.fixtures_dir or os.path.dirname(here)
    rev = args.revision

    onnx_src = os.path.join(fixtures_dir, f"{MODEL_ID}-{rev}.onnx")
    manifest_src = os.path.join(fixtures_dir, f"{MODEL_ID}-{rev}-manifest.json")
    for path in (onnx_src, manifest_src):
        if not os.path.isfile(path):
            print(f"missing fixture artifact: {path}")
            return 1

    fixture_manifest = json.load(open(manifest_src))

    out = os.path.abspath(args.out)
    if os.path.exists(out):
        make_writable(out)
        shutil.rmtree(out)
    os.makedirs(os.path.join(out, MODEL_ID, "1"), exist_ok=True)

    # role=model
    model_rel = f"{MODEL_ID}/1/model.onnx"
    shutil.copyfile(onnx_src, os.path.join(out, model_rel))

    # role=config
    config_rel = f"{MODEL_ID}/config.pbtxt"
    with open(os.path.join(out, config_rel), "w") as f:
        f.write(CONFIG_PBTXT_R2)

    # role=bundle-manifest: the adapter/taxonomy data configuration the Gateway
    # binds at startup. Only data, never executable content.
    bundle_rel = "bundle-manifest.json"
    bundle = {
        "schema_version": "masi-model-bundle-manifest/v1",
        "model_id": fixture_manifest["model_id"],
        "revision": fixture_manifest["revision"],
        "model_digest": fixture_manifest["model_digest"],
        "bundle_digest": fixture_manifest["bundle_digest"],
        "feature_schema": fixture_manifest["feature_schema"],
        "label_taxonomy": fixture_manifest["label_taxonomy"],
        "output_adapter": fixture_manifest["output_adapter"],
        "contract_digests": fixture_manifest["contract_digests"],
        "triton": fixture_manifest["triton"],
    }
    with open(os.path.join(out, bundle_rel), "w") as f:
        json.dump(bundle, f, indent=2, sort_keys=True)

    members = [
        {
            "rel_path": model_rel,
            "member_digest": sha256_file(os.path.join(out, model_rel)),
            "role": "model",
        },
        {
            "rel_path": config_rel,
            "member_digest": sha256_file(os.path.join(out, config_rel)),
            "role": "config",
        },
        {
            "rel_path": bundle_rel,
            "member_digest": sha256_file(os.path.join(out, bundle_rel)),
            "role": "bundle-manifest",
        },
    ]
    members.sort(key=lambda m: m["rel_path"])
    digest = closure_digest(members)

    identity = f"repo-{MODEL_ID}-{rev}"
    with open(os.path.join(out, "closure-manifest.json"), "w") as f:
        json.dump(
            {
                "schema_version": "masi-repository-closure/v1",
                "identity": identity,
                "closure_digest": digest,
                "members": members,
            },
            f,
            indent=2,
        )

    make_readonly(out)

    print(json.dumps(
        {
            "repository_root": out,
            "identity": identity,
            "closure_digest": digest,
            "model_digest": members[0]["member_digest"] if members[0]["role"] == "model" else None,
            "members": members,
            "contract_digests": fixture_manifest["contract_digests"],
        },
        indent=2,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
