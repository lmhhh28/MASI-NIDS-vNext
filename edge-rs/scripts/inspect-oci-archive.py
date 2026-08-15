#!/usr/bin/env python3
"""Bounded, fail-closed inspection of one linux/amd64 Docker image archive."""

from __future__ import annotations

import argparse
import hashlib
import gzip
import json
import os
import posixpath
import re
import tarfile
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO


MAX_ARCHIVE_BYTES = 67_108_864
MAX_ARCHIVE_MEMBERS = 65_536
MAX_ARCHIVE_EXPANDED_BYTES = 536_870_912
MAX_MEMBER_BYTES = 67_108_864
MAX_INDEX_BYTES = 1_048_576
MAX_CONFIG_BYTES = 8_388_608
MAX_LAYERS = 256
MAX_LAYER_MEMBERS = 65_536
MAX_LAYER_EXPANDED_BYTES = 536_870_912
MAX_BINARY_BYTES = 67_108_864
MAX_PATH_BYTES = 1_024
DIGEST_PATH = re.compile(r"blobs/sha256/([0-9a-f]{64})")


def hash_stream(source: BinaryIO, maximum: int) -> tuple[str, bytes]:
    digest = hashlib.sha256()
    chunks: list[bytes] = []
    observed = 0
    while True:
        chunk = source.read(min(1024 * 1024, maximum - observed + 1))
        if not chunk:
            break
        observed += len(chunk)
        if observed > maximum:
            raise ValueError("archive member exceeds its byte ceiling")
        digest.update(chunk)
        chunks.append(chunk)
    return "sha256:" + digest.hexdigest(), b"".join(chunks)


def hash_only_stream(source: BinaryIO, maximum: int) -> str:
    digest = hashlib.sha256()
    observed = 0
    while True:
        chunk = source.read(min(1024 * 1024, maximum - observed + 1))
        if not chunk:
            break
        observed += len(chunk)
        if observed > maximum:
            raise ValueError("archive member exceeds its byte ceiling")
        digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def safe_member_name(value: str) -> bool:
    if not value or len(value.encode()) > MAX_PATH_BYTES:
        return False
    normalized = value.removeprefix("./")
    path = PurePosixPath(normalized)
    return (
        normalized == path.as_posix()
        and not path.is_absolute()
        and ".." not in path.parts
    )


def safe_link_target(member_name: str, link_name: str) -> bool:
    if not link_name or len(link_name.encode()) > MAX_PATH_BYTES:
        return False
    resolved = (
        posixpath.normpath(link_name.lstrip("/"))
        if PurePosixPath(link_name).is_absolute()
        else posixpath.normpath(
            posixpath.join(posixpath.dirname(member_name), link_name)
        )
    )
    return resolved != ".." and not resolved.startswith("../")


def register_unique_layer_member(names: set[str], name: str) -> None:
    if name in names:
        raise ValueError(f"OCI layer repeats filesystem path {name}")
    names.add(name)


def write_exclusive(path: Path, payload: bytes) -> None:
    if path.is_symlink() or path.exists():
        raise ValueError(f"refusing to replace archive inspection output {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
    finally:
        os.close(descriptor)


def require_object(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} is not an object")
    return value


def descriptor_blob(
    archive: tarfile.TarFile,
    members: dict[str, tarfile.TarInfo],
    descriptor: object,
    *,
    label: str,
    maximum: int,
) -> tuple[dict[str, Any], str, bytes]:
    document = require_object(descriptor, f"{label} descriptor")
    digest = document.get("digest")
    size = document.get("size")
    if (
        not isinstance(digest, str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None
        or not isinstance(size, int)
        or isinstance(size, bool)
        or not 0 < size <= maximum
    ):
        raise ValueError(f"{label} descriptor digest or size is invalid")
    path = "blobs/sha256/" + digest.removeprefix("sha256:")
    member = members.get(path)
    if member is None or not member.isfile() or member.size != size:
        raise ValueError(f"{label} descriptor does not bind a regular exact-size blob")
    stream = archive.extractfile(member)
    if stream is None:
        raise ValueError(f"{label} descriptor blob is unreadable")
    observed_digest, payload = hash_stream(stream, maximum)
    if observed_digest != digest:
        raise ValueError(f"{label} descriptor blob digest mismatches")
    return document, path, payload


def json_payload(payload: bytes, label: str) -> dict[str, Any]:
    return require_object(
        json.loads(
            payload,
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
        ),
        label,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--image-ref", required=True)
    parser.add_argument("--index-output", type=Path, required=True)
    parser.add_argument("--config-output", type=Path, required=True)
    parser.add_argument("--summary-output", type=Path, required=True)
    args = parser.parse_args()

    archive_path = args.archive
    if (
        archive_path.is_symlink()
        or not archive_path.is_file()
        or archive_path.stat().st_size < 1
        or archive_path.stat().st_size > MAX_ARCHIVE_BYTES
    ):
        raise SystemExit("OCI archive is missing, symbolic, empty, or oversized")
    if (
        not args.image_ref
        or len(args.image_ref.encode()) > 512
        or re.search(r"\s", args.image_ref)
    ):
        raise SystemExit("OCI image reference is invalid")

    names: set[str] = set()
    members: dict[str, tarfile.TarInfo] = {}
    member_count = 0
    expanded_bytes = 0
    binary_digest = ""
    binary_bytes = 0
    binary_path = "usr/local/bin/masi-edge"
    binary_ancestors = ("usr", "usr/local", "usr/local/bin")
    binary_ancestor_state: dict[str, str] = {}
    layer_member_count = 0
    layer_expanded_bytes = 0
    try:
        with tarfile.open(archive_path, "r:") as archive:
            for member in archive:
                member_count += 1
                name = member.name.removeprefix("./")
                if (
                    member_count > MAX_ARCHIVE_MEMBERS
                    or not safe_member_name(member.name)
                    or name in names
                    or not (member.isfile() or member.isdir())
                ):
                    raise ValueError(
                        "OCI archive has an unsafe, duplicate, or special member"
                    )
                names.add(name)
                members[name] = member
                if member.isfile():
                    if member.size < 0 or member.size > MAX_MEMBER_BYTES:
                        raise ValueError("OCI archive member exceeds its byte ceiling")
                    expanded_bytes += member.size
                    if expanded_bytes > MAX_ARCHIVE_EXPANDED_BYTES:
                        raise ValueError(
                            "OCI archive exceeds its expanded byte ceiling"
                        )

            layout_member = members.get("oci-layout")
            top_index_member = members.get("index.json")
            if (
                layout_member is None
                or not layout_member.isfile()
                or not 0 < layout_member.size <= MAX_INDEX_BYTES
                or top_index_member is None
                or not top_index_member.isfile()
                or not 0 < top_index_member.size <= MAX_INDEX_BYTES
            ):
                raise ValueError("OCI archive layout or top-level index is missing")
            layout_stream = archive.extractfile(layout_member)
            top_index_stream = archive.extractfile(top_index_member)
            if layout_stream is None or top_index_stream is None:
                raise ValueError("OCI archive layout or top-level index is unreadable")
            layout_digest, layout_bytes = hash_stream(layout_stream, MAX_INDEX_BYTES)
            top_index_digest, top_index_bytes = hash_stream(
                top_index_stream, MAX_INDEX_BYTES
            )
            layout = json_payload(layout_bytes, "OCI layout")
            top_index = json_payload(top_index_bytes, "OCI top-level index")
            if layout != {"imageLayoutVersion": "1.0.0"}:
                raise ValueError("OCI layout version drifted")
            top_descriptors = top_index.get("manifests")
            if (
                top_index.get("schemaVersion") != 2
                or top_index.get("mediaType")
                != "application/vnd.oci.image.index.v1+json"
                or not isinstance(top_descriptors, list)
                or len(top_descriptors) != 1
            ):
                raise ValueError("OCI top-level index shape drifted")
            outer_descriptor, outer_index_path, outer_index_bytes = descriptor_blob(
                archive,
                members,
                top_descriptors[0],
                label="OCI image index",
                maximum=MAX_INDEX_BYTES,
            )
            outer_media_type = outer_descriptor.get("mediaType")
            if outer_media_type == "application/vnd.oci.image.index.v1+json":
                outer_index = json_payload(outer_index_bytes, "OCI image index")
                child_descriptors = outer_index.get("manifests")
                if (
                    outer_index.get("schemaVersion") != 2
                    or outer_index.get("mediaType")
                    != "application/vnd.oci.image.index.v1+json"
                    or not isinstance(child_descriptors, list)
                    or not 1 <= len(child_descriptors) <= 2
                ):
                    raise ValueError("OCI image index child inventory drifted")
                image_descriptors = [
                    value
                    for value in child_descriptors
                    if isinstance(value, dict)
                    and value.get("platform")
                    == {"architecture": "amd64", "os": "linux"}
                    and value.get("mediaType")
                    == "application/vnd.oci.image.manifest.v1+json"
                ]
                attestation_descriptors = [
                    value for value in child_descriptors if value not in image_descriptors
                ]
                if len(image_descriptors) != 1 or len(attestation_descriptors) > 1:
                    raise ValueError(
                        "OCI image index must contain one linux/amd64 image"
                    )
                image_descriptor, image_manifest_path, image_manifest_bytes = (
                    descriptor_blob(
                        archive,
                        members,
                        image_descriptors[0],
                        label="OCI linux/amd64 image manifest",
                        maximum=MAX_INDEX_BYTES,
                    )
                )
            elif (
                outer_media_type
                == "application/vnd.docker.distribution.manifest.v2+json"
            ):
                image_descriptor = outer_descriptor
                image_manifest_path = outer_index_path
                image_manifest_bytes = outer_index_bytes
                attestation_descriptors = []
            else:
                raise ValueError("OCI top-level image descriptor media type drifted")
            image_manifest = json_payload(
                image_manifest_bytes, "OCI linux/amd64 image manifest"
            )
            image_config_descriptor = image_manifest.get("config")
            image_layer_descriptors = image_manifest.get("layers")
            image_manifest_media_type = image_manifest.get("mediaType")
            if image_manifest_media_type == "application/vnd.oci.image.manifest.v1+json":
                expected_config_media_type = "application/vnd.oci.image.config.v1+json"
                expected_layer_media_type = "application/vnd.oci.image.layer.v1.tar+gzip"
            elif (
                image_manifest_media_type
                == "application/vnd.docker.distribution.manifest.v2+json"
            ):
                expected_config_media_type = (
                    "application/vnd.docker.container.image.v1+json"
                )
                expected_layer_media_type = (
                    "application/vnd.docker.image.rootfs.diff.tar.gzip"
                )
            else:
                raise ValueError("OCI image manifest media type drifted")
            if (
                image_manifest.get("schemaVersion") != 2
                or not isinstance(image_config_descriptor, dict)
                or image_config_descriptor.get("mediaType")
                != expected_config_media_type
                or not isinstance(image_layer_descriptors, list)
                or not 1 <= len(image_layer_descriptors) <= MAX_LAYERS
                or any(
                    not isinstance(value, dict)
                    or value.get("mediaType") != expected_layer_media_type
                    for value in image_layer_descriptors
                )
            ):
                raise ValueError("OCI linux/amd64 image manifest shape drifted")
            referenced_blob_paths = {outer_index_path, image_manifest_path}
            _, descriptor_config_path, descriptor_config_bytes = descriptor_blob(
                archive,
                members,
                image_config_descriptor,
                label="OCI image config",
                maximum=MAX_CONFIG_BYTES,
            )
            referenced_blob_paths.add(descriptor_config_path)
            descriptor_layer_paths: list[str] = []
            for position, descriptor in enumerate(image_layer_descriptors):
                _, layer_path, _ = descriptor_blob(
                    archive,
                    members,
                    descriptor,
                    label=f"OCI image layer {position}",
                    maximum=MAX_MEMBER_BYTES,
                )
                referenced_blob_paths.add(layer_path)
                descriptor_layer_paths.append(layer_path)

            for position, descriptor in enumerate(attestation_descriptors):
                if (
                    not isinstance(descriptor, dict)
                    or descriptor.get("mediaType")
                    != "application/vnd.oci.image.manifest.v1+json"
                    or descriptor.get("platform")
                    != {"architecture": "unknown", "os": "unknown"}
                    or not isinstance(descriptor.get("annotations"), dict)
                    or descriptor["annotations"].get("vnd.docker.reference.type")
                    != "attestation-manifest"
                    or descriptor["annotations"].get("vnd.docker.reference.digest")
                    != image_descriptor.get("digest")
                ):
                    raise ValueError("OCI attestation descriptor shape drifted")
                _, attestation_path, attestation_bytes = descriptor_blob(
                    archive,
                    members,
                    descriptor,
                    label=f"OCI attestation manifest {position}",
                    maximum=MAX_INDEX_BYTES,
                )
                referenced_blob_paths.add(attestation_path)
                attestation = json_payload(
                    attestation_bytes, f"OCI attestation manifest {position}"
                )
                attestation_config = attestation.get("config")
                attestation_layers = attestation.get("layers")
                if (
                    attestation.get("schemaVersion") != 2
                    or attestation.get("mediaType")
                    != "application/vnd.oci.image.manifest.v1+json"
                    or not isinstance(attestation_config, dict)
                    or not isinstance(attestation_layers, list)
                    or len(attestation_layers) != 1
                    or not isinstance(attestation_layers[0], dict)
                    or attestation_layers[0].get("mediaType")
                    != "application/vnd.in-toto+json"
                    or not isinstance(attestation_layers[0].get("annotations"), dict)
                    or attestation_layers[0]["annotations"].get(
                        "in-toto.io/predicate-type"
                    )
                    != "https://slsa.dev/provenance/v1"
                ):
                    raise ValueError("OCI attestation manifest shape drifted")
                for label, descriptor_value in (
                    ("attestation config", attestation_config),
                    ("attestation statement", attestation_layers[0]),
                ):
                    _, referenced, _ = descriptor_blob(
                        archive,
                        members,
                        descriptor_value,
                        label=f"OCI {label}",
                        maximum=MAX_CONFIG_BYTES,
                    )
                    referenced_blob_paths.add(referenced)

            index_member = members.get("manifest.json")
            if (
                index_member is None
                or not index_member.isfile()
                or index_member.size < 1
                or index_member.size > MAX_INDEX_BYTES
            ):
                raise ValueError("OCI archive has no bounded regular manifest.json")
            index_stream = archive.extractfile(index_member)
            if index_stream is None:
                raise ValueError("OCI archive manifest.json is unreadable")
            index_digest, index_bytes = hash_stream(index_stream, MAX_INDEX_BYTES)
            index = json.loads(
                index_bytes,
                parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
            )
            if not isinstance(index, list) or len(index) != 1:
                raise ValueError("OCI archive must contain exactly one image")
            image = require_object(index[0], "OCI archive image")
            if set(image) != {"Config", "RepoTags", "Layers"}:
                raise ValueError("OCI archive image index fields drifted")
            config_path = image.get("Config")
            repo_tags = image.get("RepoTags")
            layers = image.get("Layers")
            config_match = (
                DIGEST_PATH.fullmatch(config_path)
                if isinstance(config_path, str)
                else None
            )
            if config_match is None:
                raise ValueError("OCI archive config path is not content-addressed")
            if repo_tags != [args.image_ref]:
                raise ValueError("OCI archive does not bind the exact image tag")
            if (
                not isinstance(layers, list)
                or not 1 <= len(layers) <= MAX_LAYERS
                or len(layers) != len(set(layers))
                or any(
                    not isinstance(layer, str) or DIGEST_PATH.fullmatch(layer) is None
                    for layer in layers
                )
            ):
                raise ValueError("OCI archive layer inventory is invalid")
            for referenced in [config_path, *layers]:
                referenced_member = members.get(referenced)
                if referenced_member is None or not referenced_member.isfile():
                    raise ValueError(
                        "OCI archive references a missing/non-regular blob"
                    )
            regular_names = {
                name for name, member in members.items() if member.isfile()
            }
            if regular_names != {
                "manifest.json",
                "index.json",
                "oci-layout",
                *referenced_blob_paths,
            }:
                raise ValueError("OCI archive regular-file closure is not exact")
            if (
                config_path != descriptor_config_path
                or layers != descriptor_layer_paths
            ):
                raise ValueError("legacy archive index disagrees with OCI descriptors")
            for layer in layers:
                layer_stream = archive.extractfile(members[layer])
                if layer_stream is None:
                    raise ValueError("OCI archive layer blob is unreadable")
                if hash_only_stream(layer_stream, MAX_MEMBER_BYTES) != (
                    "sha256:" + DIGEST_PATH.fullmatch(layer).group(1)
                ):
                    raise ValueError(
                        "OCI archive layer blob digest does not match its path"
                    )

            config_member = members[config_path]
            if config_member.size < 1 or config_member.size > MAX_CONFIG_BYTES:
                raise ValueError("OCI config blob exceeds its byte ceiling")
            config_stream = archive.extractfile(config_member)
            if config_stream is None:
                raise ValueError("OCI config blob is unreadable")
            config_digest, config_bytes = hash_stream(config_stream, MAX_CONFIG_BYTES)
            if config_bytes != descriptor_config_bytes:
                raise ValueError("legacy config blob disagrees with OCI descriptor")
            config = require_object(
                json.loads(
                    config_bytes,
                    parse_constant=lambda value: (_ for _ in ()).throw(
                        ValueError(value)
                    ),
                ),
                "OCI config",
            )
            rootfs_document = require_object(config.get("rootfs"), "OCI rootfs")
            diff_ids = rootfs_document.get("diff_ids")
            if (
                not isinstance(diff_ids, list)
                or len(diff_ids) != len(layers)
                or any(
                    not isinstance(value, str)
                    or re.fullmatch(r"sha256:[0-9a-f]{64}", value) is None
                    for value in diff_ids
                )
            ):
                raise ValueError("OCI config diff_id inventory is invalid")
            for layer_path, expected_diff_id in zip(layers, diff_ids, strict=True):
                compressed = archive.extractfile(members[layer_path])
                if compressed is None or compressed.read(2) != b"\x1f\x8b":
                    raise ValueError("OCI layer does not use the frozen gzip profile")
                compressed = archive.extractfile(members[layer_path])
                if compressed is None:
                    raise ValueError("OCI layer blob is unreadable")
                diff_hasher = hashlib.sha256()
                uncompressed_observed = 0
                with gzip.GzipFile(fileobj=compressed, mode="rb") as uncompressed:
                    while True:
                        chunk = uncompressed.read(1024 * 1024)
                        if not chunk:
                            break
                        uncompressed_observed += len(chunk)
                        if uncompressed_observed > MAX_LAYER_EXPANDED_BYTES:
                            raise ValueError(
                                "OCI layer exceeds its expanded byte ceiling"
                            )
                        diff_hasher.update(chunk)
                if "sha256:" + diff_hasher.hexdigest() != expected_diff_id:
                    raise ValueError("OCI layer does not match its config diff_id")

                compressed = archive.extractfile(members[layer_path])
                if compressed is None:
                    raise ValueError("OCI layer blob is unreadable")
                layer_names: set[str] = set()
                with tarfile.open(fileobj=compressed, mode="r|gz") as layer_archive:
                    for layer_member in layer_archive:
                        layer_member_count += 1
                        layer_name = layer_member.name.removeprefix("./")
                        if (
                            layer_member_count > MAX_LAYER_MEMBERS
                            or not safe_member_name(layer_member.name)
                            or not (
                                layer_member.isfile()
                                or layer_member.isdir()
                                or layer_member.issym()
                                or layer_member.islnk()
                            )
                            or (
                                (layer_member.issym() or layer_member.islnk())
                                and not safe_link_target(
                                    layer_name, layer_member.linkname
                                )
                            )
                        ):
                            raise ValueError("OCI layer has an unsafe member")
                        register_unique_layer_member(layer_names, layer_name)
                        if layer_member.isfile():
                            layer_expanded_bytes += layer_member.size
                            if (
                                layer_member.size < 0
                                or layer_member.size > MAX_MEMBER_BYTES
                                or layer_expanded_bytes > MAX_LAYER_EXPANDED_BYTES
                            ):
                                raise ValueError(
                                    "OCI layer filesystem exceeds its resource ceiling"
                                )
                        basename = posixpath.basename(layer_name)
                        if basename == ".wh..wh..opq":
                            opaque_directory = posixpath.dirname(layer_name).rstrip("/")
                            if binary_path.startswith(opaque_directory + "/"):
                                binary_digest = ""
                                binary_bytes = 0
                            if opaque_directory in binary_ancestors:
                                binary_ancestor_state[opaque_directory] = "directory"
                            for ancestor in binary_ancestors:
                                if ancestor.startswith(opaque_directory + "/"):
                                    binary_ancestor_state[ancestor] = "missing"
                        elif basename.startswith(".wh."):
                            removed = posixpath.join(
                                posixpath.dirname(layer_name),
                                basename.removeprefix(".wh."),
                            )
                            if removed == binary_path or binary_path.startswith(
                                removed.rstrip("/") + "/"
                            ):
                                binary_digest = ""
                                binary_bytes = 0
                            for ancestor in binary_ancestors:
                                if ancestor == removed or ancestor.startswith(
                                    removed.rstrip("/") + "/"
                                ):
                                    binary_ancestor_state[ancestor] = "missing"
                        elif layer_name in binary_ancestors:
                            if layer_member.isdir():
                                binary_ancestor_state[layer_name] = "directory"
                            else:
                                binary_ancestor_state[layer_name] = "blocked"
                                binary_digest = ""
                                binary_bytes = 0
                        elif layer_name == binary_path:
                            if (
                                not layer_member.isfile()
                                or layer_member.size < 1
                                or layer_member.size > MAX_BINARY_BYTES
                            ):
                                raise ValueError("OCI Edge binary member is invalid")
                            if any(
                                binary_ancestor_state.get(ancestor)
                                in {"blocked", "missing"}
                                for ancestor in binary_ancestors
                            ):
                                raise ValueError(
                                    "OCI Edge binary has a non-directory or removed ancestor"
                                )
                            binary = layer_archive.extractfile(layer_member)
                            if binary is None:
                                raise ValueError("OCI Edge binary member is unreadable")
                            binary_digest, binary_payload = hash_stream(
                                binary, MAX_BINARY_BYTES
                            )
                            binary_bytes = len(binary_payload)
    except (OSError, tarfile.TarError, ValueError, json.JSONDecodeError) as error:
        raise SystemExit(f"invalid OCI archive: {error}") from error

    expected_config_digest = "sha256:" + config_match.group(1)
    if config_digest != expected_config_digest:
        raise SystemExit(
            "OCI config blob digest does not match its content-addressed path"
        )
    runtime_config = require_object(config.get("config"), "OCI runtime config")
    rootfs = require_object(config.get("rootfs"), "OCI rootfs")
    diff_ids = rootfs.get("diff_ids")
    if (
        config.get("architecture") != "amd64"
        or config.get("os") != "linux"
        or runtime_config.get("User") != "65532:65532"
        or runtime_config.get("Entrypoint") != ["/usr/local/bin/masi-edge"]
        or rootfs.get("type") != "layers"
        or not isinstance(diff_ids, list)
        or len(diff_ids) != len(layers)
    ):
        raise SystemExit("OCI config platform, user, entrypoint, or rootfs drifted")
    if not binary_digest or binary_bytes < 1:
        raise SystemExit("OCI archive does not resolve an exact Edge binary")

    archive_digest = "sha256:"
    archive_hasher = hashlib.sha256()
    with archive_path.open("rb") as archive_source:
        for chunk in iter(lambda: archive_source.read(1024 * 1024), b""):
            archive_hasher.update(chunk)
    archive_digest += archive_hasher.hexdigest()

    summary = {
        "schema_version": "edge-oci-archive-inspection/v1",
        "image_ref": args.image_ref,
        "platform": "linux/amd64",
        "image_user": "65532:65532",
        "entrypoint": ["/usr/local/bin/masi-edge"],
        "archive_digest": archive_digest,
        "archive_bytes": archive_path.stat().st_size,
        "archive_member_count": member_count,
        "archive_expanded_bytes": expanded_bytes,
        "manifest_digest": outer_descriptor["digest"],
        "manifest_media_type": image_manifest_media_type,
        "image_manifest_descriptor_digest": image_descriptor["digest"],
        "attestation_count": len(attestation_descriptors),
        "oci_layout_digest": layout_digest,
        "oci_index_digest": top_index_digest,
        "index_path": "image-archive-manifest.json",
        "index_digest": index_digest,
        "index_bytes": len(index_bytes),
        "config_path": "image-config.json",
        "config_blob_path": config_path,
        "config_digest": config_digest,
        "config_bytes": len(config_bytes),
        "layer_count": len(layers),
        "layer_compression": "gzip",
        "layer_member_count": layer_member_count,
        "layer_expanded_bytes": layer_expanded_bytes,
        "binary_path": binary_path,
        "binary_digest": binary_digest,
        "binary_bytes": binary_bytes,
        "resource_limits": {
            "archive_bytes": MAX_ARCHIVE_BYTES,
            "archive_members": MAX_ARCHIVE_MEMBERS,
            "archive_expanded_bytes": MAX_ARCHIVE_EXPANDED_BYTES,
            "member_bytes": MAX_MEMBER_BYTES,
            "index_bytes": MAX_INDEX_BYTES,
            "config_bytes": MAX_CONFIG_BYTES,
            "layers": MAX_LAYERS,
            "layer_members": MAX_LAYER_MEMBERS,
            "layer_expanded_bytes": MAX_LAYER_EXPANDED_BYTES,
            "binary_bytes": MAX_BINARY_BYTES,
            "path_bytes": MAX_PATH_BYTES,
        },
    }
    summary_bytes = (json.dumps(summary, indent=2, sort_keys=True) + "\n").encode()
    try:
        write_exclusive(args.index_output, index_bytes)
        write_exclusive(args.config_output, config_bytes)
        write_exclusive(args.summary_output, summary_bytes)
    except (OSError, ValueError) as error:
        raise SystemExit(str(error)) from error
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
