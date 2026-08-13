from __future__ import annotations

import argparse
import ast
from pathlib import Path


MODULES = (
    "p4/config/v1/p4types_pb2.py",
    "p4/config/v1/p4info_pb2.py",
    "p4/v1/p4data_pb2.py",
    "p4/v1/p4runtime_pb2.py",
)


def descriptor_bytes(source: str, path: Path) -> bytes:
    tree = ast.parse(source, filename=str(path))
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(
            isinstance(target, ast.Name) and target.id == "DESCRIPTOR"
            for target in node.targets
        ):
            continue
        if not isinstance(node.value, ast.Call):
            continue
        for keyword in node.value.keywords:
            if keyword.arg != "serialized_pb":
                continue
            value = ast.literal_eval(keyword.value)
            if isinstance(value, bytes):
                return value
    raise ValueError(f"{path} has no bytes-valued DESCRIPTOR serialized_pb")


def dependency_imports(source: str, path: Path) -> list[str]:
    tree = ast.parse(source, filename=str(path))
    imports: list[str] = []
    for node in tree.body:
        if not isinstance(node, ast.ImportFrom) or node.module is None:
            continue
        aliases = [alias for alias in node.names if alias.name.endswith("_pb2")]
        if not aliases:
            continue
        names = ", ".join(
            alias.name if alias.asname is None else f"{alias.name} as {alias.asname}"
            for alias in aliases
        )
        imports.append(f"from {node.module} import {names}")
    return imports


def modern_module(source: str, path: Path, module_name: str) -> str:
    serialized = descriptor_bytes(source, path)
    dependencies = dependency_imports(source, path)
    lines = [
        "# -*- coding: utf-8 -*-",
        "# Adapted from p4runtime 1.4.1 descriptors for protobuf 6.x.",
        "from google.protobuf import descriptor_pool as _descriptor_pool",
        "from google.protobuf import symbol_database as _symbol_database",
        "from google.protobuf.internal import builder as _builder",
        *dependencies,
        "",
        "_sym_db = _symbol_database.Default()",
        f"DESCRIPTOR = _descriptor_pool.Default().AddSerializedFile({serialized!r})",
        "_builder.BuildMessageAndEnumDescriptors(DESCRIPTOR, globals())",
        f"_builder.BuildTopDescriptorsAndMessages(DESCRIPTOR, {module_name!r}, globals())",
        "# @@protoc_insertion_point(module_scope)",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("site_packages", type=Path)
    args = parser.parse_args()
    for relative in MODULES:
        path = args.site_packages / relative
        source = path.read_text(encoding="utf-8")
        module_name = relative.removesuffix(".py").replace("/", ".")
        path.write_text(modern_module(source, path, module_name), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
