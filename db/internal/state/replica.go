package state

import (
	"context"
	"errors"
	"fmt"

	"github.com/jackc/pgx/v5"
)

type ReplicaInspection struct {
	Database          string `json:"database"`
	TLS               bool   `json:"tls"`
	InRecovery        bool   `json:"in_recovery"`
	TimelineID        int64  `json:"timeline_id"`
	MarkerPresent     bool   `json:"marker_present"`
	SchemaVersion     string `json:"schema_version"`
	SchemaChainDigest string `json:"schema_chain_digest"`
	TargetWriter      bool   `json:"target_writer"`
	ModelWriter       bool   `json:"model_writer"`
}

func InspectReplica(ctx context.Context, dsn, confirmedDatabase, marker string,
	requireTLS, expectRecovery bool) (*ReplicaInspection, error) {
	if marker == "" {
		return nil, errors.New("replica inspect: marker required")
	}
	conn, err := pgx.Connect(ctx, dsn)
	if err != nil {
		return nil, fmt.Errorf("replica inspect: connect: %w", err)
	}
	defer conn.Close(context.Background())
	out := &ReplicaInspection{}
	if err := conn.QueryRow(ctx, `SELECT current_database(),
		EXISTS(SELECT 1 FROM pg_stat_ssl WHERE pid=pg_backend_pid() AND ssl),
		pg_is_in_recovery(),CASE WHEN pg_is_in_recovery()
		THEN COALESCE((pg_control_checkpoint()).timeline_id,1)
		ELSE (('x'||substring(pg_walfile_name(pg_current_wal_lsn()),1,8))::bit(32)::bigint) END,
		EXISTS(SELECT 1 FROM retention_holds WHERE hold_id=$1),meta.value,meta.checksum,
		t.writer_enabled,m.writer_enabled
	FROM masi_schema_meta meta CROSS JOIN target_control_state t CROSS JOIN model_control_state m
	WHERE meta.key='version' AND t.singleton AND m.singleton`, marker).Scan(
		&out.Database, &out.TLS, &out.InRecovery, &out.TimelineID, &out.MarkerPresent,
		&out.SchemaVersion, &out.SchemaChainDigest, &out.TargetWriter, &out.ModelWriter); err != nil {
		return nil, fmt.Errorf("replica inspect: state: %w", err)
	}
	if out.Database != confirmedDatabase || (requireTLS && !out.TLS) ||
		out.InRecovery != expectRecovery || !out.MarkerPresent || out.SchemaVersion != "21" ||
		out.TargetWriter || out.ModelWriter {
		return nil, errors.New("replica inspect: identity, recovery, marker, schema or writer oracle mismatch")
	}
	return out, nil
}
