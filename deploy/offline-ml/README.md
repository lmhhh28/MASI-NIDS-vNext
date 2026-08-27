# Offline ML acceptance job

This deployment asset runs `MOD-ML-001` as a bounded terminating job. It has no
network, database, P4, device, model-current, or effect credentials. The caller
must provide an immutable `repository@sha256` image and an existing empty
absolute output parent owned for UID/GID 65532. Success is the job's exit `0`
plus independent `masi-offline-ml verify`; Compose `running` is not evidence.

The root filesystem is read-only, all capabilities are dropped,
no-new-privileges is enabled, and CPU/memory/PID/tmpfs budgets are explicit.
There is intentionally no service health endpoint because the process exits
after atomic publication.
