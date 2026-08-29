#!/usr/bin/env python3
"""Run a deployable smoke under one total deadline and persist failure evidence."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import selectors
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from testkit.p4_switch.lib.source_identity import read_stable_regular_file


MODULE_IDS = {
    "p4": "MOD-SW-001",
    "edge": "MOD-EDGE-001",
    "inference": "MOD-INF-001",
    "control": "MOD-CTRL-001",
    "db": "MOD-DB-001",
    "plugin-host": "MOD-PLUGIN-001",
    "analysis": "MOD-AGENT-001",
    "offline-ml": "MOD-ML-001",
    "web": "MOD-WEB-001",
}
MODULE_ROOTS = {
    "p4": "evidence/runtime-smoke-failures/p4",
    "edge": "edge-rs/evidence/runtime-smoke-failures",
    "inference": "infer-cpp/evidence/runtime-smoke-failures",
    "control": "control-go/evidence/runtime-smoke-failures",
    "db": "db/evidence/runtime-smoke-failures",
    "plugin-host": "plugin-host-rs/evidence/runtime-smoke-failures",
    "analysis": "analysis-py/evidence/runtime-smoke-failures",
    "offline-ml": "ml-py/evidence/runtime-smoke-failures",
    "web": "web/evidence/runtime-smoke-failures",
}
EXCLUDED_PARTS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
    "build",
    "dist",
    "evidence",
    "node_modules",
    "out",
    "output",
    "target",
}
MAXIMUM_LOG_BYTES = 64 * 1024 * 1024
MAXIMUM_DRAIN_BYTES_PER_TICK = 1024 * 1024
PIPE_HOLDER_TERMINATION_SECONDS = 2.0


def sha256(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def configure_stdout_forwarding() -> tuple[int | None, bool | None]:
    """Return a non-blocking stdout fd for best-effort live log forwarding.

    The persisted temporary log is authoritative.  A closed or backpressured
    caller stdout must never suspend the total runtime deadline or prevent the
    structured failure sidecar from being written.
    """

    try:
        descriptor = sys.stdout.fileno()
        was_blocking = os.get_blocking(descriptor)
        os.set_blocking(descriptor, False)
        return descriptor, was_blocking
    except (AttributeError, OSError, ValueError):
        return None, None


def forward_stdout(descriptor: int | None, payload: bytes) -> int | None:
    """Forward at most one non-blocking chunk; disable on pressure or closure."""

    if descriptor is None or not payload:
        return descriptor
    try:
        written = os.write(descriptor, payload)
    except (BlockingIOError, BrokenPipeError, OSError):
        return None
    return descriptor if written == len(payload) else None


def restore_stdout_blocking(descriptor: int | None, was_blocking: bool | None) -> None:
    if descriptor is None or was_blocking is None:
        return
    try:
        os.set_blocking(descriptor, was_blocking)
    except OSError:
        pass


def signal_process_group(pid: int, sig: signal.Signals) -> None:
    try:
        os.killpg(pid, sig)
    except ProcessLookupError:
        pass


def pipe_holder_pids(pipe_inode: int) -> set[int]:
    """Find exact Linux processes retaining this wrapper's unique stdout pipe."""

    expected = f"pipe:[{pipe_inode}]"
    holders: set[int] = set()
    proc = Path("/proc")
    if not proc.is_dir():
        return holders
    for process_dir in proc.iterdir():
        if not process_dir.name.isdigit() or int(process_dir.name) == os.getpid():
            continue
        descriptor_dir = process_dir / "fd"
        try:
            for descriptor in descriptor_dir.iterdir():
                try:
                    if os.readlink(descriptor) == expected:
                        holders.add(int(process_dir.name))
                        break
                except (FileNotFoundError, PermissionError, OSError):
                    continue
        except (FileNotFoundError, PermissionError, OSError):
            continue
    return holders


def terminate_pipe_holders(pipe_inode: int) -> None:
    holders = pipe_holder_pids(pipe_inode)
    for pid in holders:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + PIPE_HOLDER_TERMINATION_SECONDS
    while holders and time.monotonic() < deadline:
        holders = {pid for pid in holders if Path(f"/proc/{pid}").exists()}
        if holders:
            time.sleep(0.05)
    for pid in holders:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def source_identity(repo: Path) -> dict[str, object]:
    revision = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        timeout=30,
    ).stdout.strip()
    status = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain=v1", "-z", "--untracked-files=all"],
        check=True,
        stdout=subprocess.PIPE,
        timeout=60,
    ).stdout
    def paths() -> list[Path]:
        names = subprocess.run(
            ["git", "-C", str(repo), "ls-files", "-co", "--exclude-standard", "-z"],
            check=True,
            stdout=subprocess.PIPE,
            timeout=60,
        ).stdout.split(b"\0")
        output: list[Path] = []
        for raw_name in sorted(value for value in names if value):
            relative = Path(os.fsdecode(raw_name))
            if EXCLUDED_PARTS.intersection(relative.parts):
                continue
            path = repo / relative
            if path.is_symlink():
                raise ValueError(f"source closure contains a symlink: {relative}")
            if path.is_file():
                output.append(path)
        return output

    source_paths = paths()
    before = {
        path: (
            (metadata := path.stat(follow_symlinks=False)).st_dev,
            metadata.st_ino,
            metadata.st_size,
            metadata.st_mtime_ns,
        )
        for path in source_paths
    }
    digest = hashlib.sha256()
    for path in source_paths:
        relative = path.relative_to(repo)
        payload = read_stable_regular_file(path)
        encoded = relative.as_posix().encode()
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    if paths() != source_paths:
        raise ValueError("runtime source file set changed while hashing")
    for path in source_paths:
        metadata = path.stat(follow_symlinks=False)
        if before[path] != (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_size,
            metadata.st_mtime_ns,
        ):
            raise ValueError(f"runtime source changed during hashing: {path}")
    return {
        "source_revision": revision,
        "source_tree_digest": "sha256:" + digest.hexdigest(),
        "working_tree_dirty": bool(status),
        "working_tree_status_digest": sha256(status),
    }


def write_new(path: Path, payload: bytes) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
        0o600,
    )
    try:
        offset = 0
        while offset < len(payload):
            offset += os.write(descriptor, payload[offset:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def prepare_failure_root(path: Path) -> Path:
    absolute = path.absolute()
    if absolute == Path(absolute.anchor):
        raise ValueError("runtime failure root cannot be a filesystem root")
    absolute.mkdir(parents=True, exist_ok=True)
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        if current.is_symlink() or not current.is_dir():
            raise ValueError("runtime failure root traverses an unsafe component")
    return absolute


def persist_failure(
    *,
    repo: Path,
    failure_root: Path,
    run_id: str,
    module: str,
    generated: dt.datetime,
    timeout_seconds: int,
    timed_out: bool,
    exit_code: int,
    stable_reason: str,
    identity: dict[str, object],
    argv: list[str],
    log_payload: bytes,
    truncated: bool,
) -> Path:
    result = "HOLD" if exit_code == 2 and not timed_out else "FAIL"
    document = {
        "schema_version": "runtime-smoke-failure/v1",
        "module": module,
        "module_id": MODULE_IDS[module],
        "run_id": run_id,
        "generated_at": generated.isoformat().replace("+00:00", "Z"),
        "timeout_seconds": timeout_seconds,
        "timed_out": timed_out,
        "exit_code": exit_code,
        "result": result,
        "qualification": "NOT_QUALIFIED",
        "stable_reason": stable_reason,
        **identity,
        "argv": argv,
        "log": {
            "path": "runtime-smoke.log",
            "digest": sha256(log_payload),
            "bytes": len(log_payload),
            "truncated": truncated,
            "maximum_bytes": MAXIMUM_LOG_BYTES,
        },
    }
    schema = json.loads(
        (repo / "contracts/evidence/runtime-smoke-failure/v1/schema.json").read_text(encoding="utf-8")
    )
    errors = sorted(
        Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(document),
        key=lambda error: list(error.absolute_path),
    )
    if errors:
        raise ValueError("; ".join(error.message for error in errors))
    final = failure_root / run_id
    if final.exists() or final.is_symlink():
        raise ValueError("runtime failure run directory already exists")
    staging = Path(tempfile.mkdtemp(prefix=f".{run_id}.", dir=failure_root))
    try:
        write_new(staging / "runtime-smoke.log", log_payload)
        write_new(
            staging / "runtime-smoke-failure.json",
            (json.dumps(document, indent=2, sort_keys=True) + "\n").encode(),
        )
        fsync_directory(staging)
        os.rename(staging, final)
        fsync_directory(failure_root)
    except BaseException:
        if staging.exists() and not staging.is_symlink():
            shutil.rmtree(staging)
        raise
    return final / "runtime-smoke-failure.json"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--module", choices=sorted(MODULE_IDS), required=True)
    parser.add_argument("--timeout-seconds", type=int, required=True)
    parser.add_argument("--failure-root", type=Path)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    arguments = parser.parse_args()
    if arguments.command and arguments.command[0] == "--":
        arguments.command = arguments.command[1:]
    if not arguments.command or not 1 <= arguments.timeout_seconds <= 21600:
        raise SystemExit("bounded runtime smoke requires a command and timeout in 1..21600")
    repo = arguments.repo.resolve(strict=True)
    if not (repo / ".git").exists():
        raise SystemExit("runtime smoke repository is not a Git checkout")
    try:
        identity = source_identity(repo)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        raise SystemExit(
            f"runtime smoke source identity unavailable; command was not started: {error}"
        ) from error

    generated = dt.datetime.now(dt.timezone.utc)
    default_run_id = f"runtime-{generated.strftime('%Y%m%dT%H%M%SZ')}-{os.getpid()}-{arguments.module}"
    run_id = os.environ.get("MASI_RUNTIME_SMOKE_RUN_ID", default_run_id)
    if (
        not 8 <= len(run_id) <= 128
        or not (run_id[0].isascii() and run_id[0].isalnum())
        or not all(
            character.isascii() and (character.isalnum() or character in "._:-")
            for character in run_id
        )
    ):
        raise SystemExit("invalid MASI_RUNTIME_SMOKE_RUN_ID; command was not started")
    if len(arguments.command) > 128 or any(len(value) > 4096 for value in arguments.command):
        raise SystemExit("runtime smoke argv exceeds the contract bound; command was not started")
    configured_failure_root = arguments.failure_root or (
        Path(os.environ["MASI_RUNTIME_FAILURE_ROOT"])
        if os.environ.get("MASI_RUNTIME_FAILURE_ROOT")
        else None
    )
    failure_root = (
        configured_failure_root.absolute()
        if configured_failure_root is not None
        else repo / MODULE_ROOTS[arguments.module]
    )
    try:
        failure_root = prepare_failure_root(failure_root)
    except ValueError as error:
        raise SystemExit(str(error)) from error
    if (failure_root / run_id).exists() or (failure_root / run_id).is_symlink():
        raise SystemExit("runtime failure run directory already exists; command was not started")

    temporary = tempfile.NamedTemporaryFile(prefix="masi-runtime-smoke-", suffix=".log", delete=False)
    temporary_path = Path(temporary.name)
    temporary.close()
    stored_bytes = 0
    truncated = False
    environment = dict(os.environ)
    environment["MASI_RUNTIME_SMOKE_WRAPPED"] = "1"
    try:
        process = subprocess.Popen(
            arguments.command,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except OSError as error:
        log_payload = f"runtime smoke launch failed: {type(error).__name__}: {error}\n".encode()
        temporary_path.unlink(missing_ok=True)
        evidence = persist_failure(
            repo=repo, failure_root=failure_root, run_id=run_id, module=arguments.module,
            generated=generated, timeout_seconds=arguments.timeout_seconds, timed_out=False,
            exit_code=127, stable_reason="RUNTIME_SMOKE_LAUNCH_FAILED", identity=identity,
            argv=arguments.command, log_payload=log_payload, truncated=False,
        )
        print(f"runtime_smoke_failure_evidence={evidence}", file=sys.stderr)
        return 127

    assert process.stdout is not None
    child_stdout_fd = process.stdout.fileno()
    pipe_inode = os.fstat(child_stdout_fd).st_ino
    os.set_blocking(child_stdout_fd, False)
    selector = selectors.DefaultSelector()
    selector.register(child_stdout_fd, selectors.EVENT_READ)
    deadline = time.monotonic() + arguments.timeout_seconds
    exit_observed_at: float | None = None
    exit_code: int | None = None
    timed_out = False
    pipe_held_open = False
    pipe_open = True
    forwarding_stdout_fd, stdout_was_blocking = configure_stdout_forwarding()
    forwarding_fd = forwarding_stdout_fd
    try:
        with temporary_path.open("wb") as stream:
            while True:
                now = time.monotonic()
                observed = process.poll()
                if observed is None and now >= deadline:
                    timed_out = True
                    signal_process_group(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        signal_process_group(process.pid, signal.SIGKILL)
                        process.wait(timeout=5)
                    exit_code = 124
                    exit_observed_at = time.monotonic()
                elif observed is not None and exit_observed_at is None:
                    exit_code = observed
                    exit_observed_at = now

                readable = selector.select(timeout=0.1) if pipe_open else []
                if not pipe_open:
                    time.sleep(0.1)
                pipe_eof = False
                drained_bytes = 0
                for _key, _events in readable:
                    while drained_bytes < MAXIMUM_DRAIN_BYTES_PER_TICK:
                        try:
                            chunk = os.read(child_stdout_fd, 64 * 1024)
                        except BlockingIOError:
                            break
                        if not chunk:
                            pipe_eof = True
                            break
                        drained_bytes += len(chunk)
                        forwarding_fd = forward_stdout(forwarding_fd, chunk)
                        remaining = MAXIMUM_LOG_BYTES - stored_bytes
                        if remaining > 0:
                            selected = chunk[:remaining]
                            stream.write(selected)
                            stored_bytes += len(selected)
                        if len(chunk) > remaining:
                            truncated = True
                    if pipe_eof:
                        break
                if pipe_eof:
                    selector.unregister(child_stdout_fd)
                    pipe_open = False
                if exit_observed_at is not None and not pipe_open:
                    break
                if pipe_open and exit_observed_at is not None and time.monotonic() - exit_observed_at >= 2:
                    pipe_held_open = True
                    truncated = True
                    terminate_pipe_holders(pipe_inode)
                    marker = b"\n[runtime wrapper: descendant retained stdout after the command exited and was terminated]\n"
                    remaining = MAXIMUM_LOG_BYTES - stored_bytes
                    if remaining > 0:
                        selected = marker[:remaining]
                        stream.write(selected)
                        stored_bytes += len(selected)
                    break
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        if process.poll() is None:
            signal_process_group(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                signal_process_group(process.pid, signal.SIGKILL)
        temporary_path.unlink(missing_ok=True)
        raise
    finally:
        selector.close()
        process.stdout.close()
        restore_stdout_blocking(forwarding_stdout_fd, stdout_was_blocking)

    if exit_code is None:
        exit_code = process.wait(timeout=5)
    if pipe_held_open and exit_code == 0:
        exit_code = 1
    if exit_code == 0:
        temporary_path.unlink(missing_ok=True)
        return 0
    if exit_code < 0:
        exit_code = min(255, 128 + abs(exit_code))
    if timed_out:
        exit_code = 124

    log_payload = temporary_path.read_bytes()
    temporary_path.unlink(missing_ok=True)
    reason = (
        "RUNTIME_SMOKE_TIMED_OUT"
        if timed_out
        else "RUNTIME_SMOKE_HOLD"
        if exit_code == 2
        else "RUNTIME_SMOKE_FAILED"
    )
    evidence = persist_failure(
        repo=repo, failure_root=failure_root, run_id=run_id, module=arguments.module,
        generated=generated, timeout_seconds=arguments.timeout_seconds, timed_out=timed_out,
        exit_code=exit_code, stable_reason=reason, identity=identity, argv=arguments.command,
        log_payload=log_payload, truncated=truncated,
    )
    print(f"runtime_smoke_failure_evidence={evidence}", file=sys.stderr)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
