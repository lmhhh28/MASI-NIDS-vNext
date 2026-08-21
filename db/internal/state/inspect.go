// Package state implements public SQL/catalog inspection for the PostgreSQL
// State qualification target. It observes PostgreSQL; it does not import Go
// Control internals or make business decisions.
package state

import (
	"context"
	"errors"
	"fmt"
	"sort"

	"github.com/jackc/pgx/v5"
)

// Inspection is a bounded, secret-free public-boundary readback.
type Inspection struct {
	Database           string            `json:"database"`
	ServerVersionNum   int               `json:"server_version_num"`
	TLS                bool              `json:"tls"`
	InRecovery         bool              `json:"in_recovery"`
	TimelineID         int64             `json:"timeline_id"`
	SchemaVersion      string            `json:"schema_version"`
	SchemaChainDigest  string            `json:"schema_chain_digest"`
	MigrationRows      int               `json:"migration_rows"`
	RequiredTables     map[string]bool   `json:"required_tables"`
	RequiredRoles      map[string]bool   `json:"required_roles"`
	Settings           map[string]string `json:"settings"`
	PartitionedParents map[string]int    `json:"partitioned_parents"`
	InvalidConstraints []string          `json:"invalid_constraints"`
	WriterGates        map[string]bool   `json:"writer_gates"`
}

var requiredTables = []string{
	"event_identities", "events", "incidents", "effect_proposals", "effect_decisions",
	"effect_intents", "effect_attempts", "firewall_revisions", "firewall_bindings",
	"rule_observation_epochs", "rule_observations", "rule_rollups_5m", "rule_rollups_1h",
	"targets", "target_assignments", "target_control_incarnations", "target_control_state",
	"fleet_operations", "fleet_child_intents", "model_control_incarnations",
	"model_control_state", "model_revisions", "pool_generations", "shard_bindings",
	"plugin_manifests", "plugin_qualifications", "plugin_bindings",
	"plugin_statistics_definitions", "plugin_statistic_schedules", "plugin_statistic_runs",
	"plugin_statistic_artifacts", "plugin_statistics_current", "plugin_statistics_history",
	"retention_holds", "database_recovery_events", "masi_schema_meta",
}

var requiredRoles = []string{
	"masi_control_app", "masi_control_readonly", "masi_control_maintenance",
	"masi_analysis_private", "masi_backup", "masi_monitoring",
}

// Inspect connects over the public PostgreSQL wire and verifies catalog/runtime
// state. requireTLS rejects sslmode=disable/prefer downgrade.
func Inspect(ctx context.Context, dsn, confirmedDatabase, historyTable string, requireTLS bool) (*Inspection, error) {
	conn, err := pgx.Connect(ctx, dsn)
	if err != nil {
		return nil, fmt.Errorf("state inspect: connect: %w", err)
	}
	defer conn.Close(context.Background())
	out := &Inspection{RequiredTables: map[string]bool{}, RequiredRoles: map[string]bool{},
		Settings: map[string]string{}, PartitionedParents: map[string]int{},
		InvalidConstraints: []string{}, WriterGates: map[string]bool{}}
	if err := conn.QueryRow(ctx, `SELECT current_database(),current_setting('server_version_num')::int,
		EXISTS(SELECT 1 FROM pg_stat_ssl WHERE pid=pg_backend_pid() AND ssl),pg_is_in_recovery(),
		CASE WHEN pg_is_in_recovery() THEN COALESCE((pg_control_checkpoint()).timeline_id,1)
		ELSE (('x'||substring(pg_walfile_name(pg_current_wal_lsn()),1,8))::bit(32)::bigint) END`).
		Scan(&out.Database, &out.ServerVersionNum, &out.TLS, &out.InRecovery, &out.TimelineID); err != nil {
		return nil, fmt.Errorf("state inspect: identity: %w", err)
	}
	if out.Database != confirmedDatabase {
		return nil, fmt.Errorf("state inspect: database %q differs from confirmation %q", out.Database, confirmedDatabase)
	}
	if out.ServerVersionNum < 180000 || out.ServerVersionNum >= 190000 {
		return nil, fmt.Errorf("state inspect: PostgreSQL 18 required, got %d", out.ServerVersionNum)
	}
	if requireTLS && !out.TLS {
		return nil, errors.New("state inspect: TLS required")
	}
	if err := conn.QueryRow(ctx, `SELECT value,checksum FROM masi_schema_meta WHERE key='version'`).
		Scan(&out.SchemaVersion, &out.SchemaChainDigest); err != nil {
		return nil, fmt.Errorf("state inspect: schema meta: %w", err)
	}
	if err := conn.QueryRow(ctx, fmt.Sprintf(`SELECT count(*) FROM %s`, historyTable)).Scan(&out.MigrationRows); err != nil {
		return nil, fmt.Errorf("state inspect: migration history: %w", err)
	}
	for _, table := range requiredTables {
		var exists bool
		if err := conn.QueryRow(ctx, `SELECT to_regclass('public.'||$1) IS NOT NULL`, table).Scan(&exists); err != nil {
			return nil, err
		}
		out.RequiredTables[table] = exists
		if !exists {
			return nil, fmt.Errorf("state inspect: required table %s missing", table)
		}
	}
	for _, role := range requiredRoles {
		var exists bool
		if err := conn.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname=$1)`, role).Scan(&exists); err != nil {
			return nil, err
		}
		out.RequiredRoles[role] = exists
		if !exists {
			return nil, fmt.Errorf("state inspect: required role %s missing", role)
		}
	}
	for _, setting := range []string{"max_connections", "shared_buffers", "wal_level", "archive_mode",
		"statement_timeout", "lock_timeout", "idle_in_transaction_session_timeout", "max_wal_size"} {
		var value string
		if err := conn.QueryRow(ctx, `SELECT current_setting($1)`, setting).Scan(&value); err != nil {
			return nil, fmt.Errorf("state inspect: setting %s: %w", setting, err)
		}
		out.Settings[setting] = value
	}
	for _, parent := range []string{"events", "rule_rollups_5m", "rule_rollups_1h", "plugin_statistics_history"} {
		var count int
		if err := conn.QueryRow(ctx, `SELECT count(*) FROM pg_inherits WHERE inhparent=to_regclass('public.'||$1)`, parent).Scan(&count); err != nil {
			return nil, err
		}
		out.PartitionedParents[parent] = count
		if count < 2 {
			return nil, fmt.Errorf("state inspect: %s partition coverage missing", parent)
		}
	}
	rows, err := conn.Query(ctx, `SELECT conrelid::regclass::text||'.'||conname FROM pg_constraint
		WHERE connamespace='public'::regnamespace AND NOT convalidated ORDER BY 1`)
	if err != nil {
		return nil, err
	}
	for rows.Next() {
		var value string
		if err := rows.Scan(&value); err != nil {
			rows.Close()
			return nil, err
		}
		out.InvalidConstraints = append(out.InvalidConstraints, value)
	}
	rows.Close()
	if len(out.InvalidConstraints) != 0 {
		sort.Strings(out.InvalidConstraints)
		return nil, fmt.Errorf("state inspect: unvalidated constraints: %v", out.InvalidConstraints)
	}
	var targetWriter, modelWriter bool
	if err := conn.QueryRow(ctx, `SELECT writer_enabled FROM target_control_state WHERE singleton`).Scan(&targetWriter); err != nil {
		return nil, err
	}
	if err := conn.QueryRow(ctx, `SELECT writer_enabled FROM model_control_state WHERE singleton`).Scan(&modelWriter); err != nil {
		return nil, err
	}
	out.WriterGates["target"] = targetWriter
	out.WriterGates["model"] = modelWriter
	return out, nil
}

// RotateRecoveredIncarnations invokes the one public maintenance function. It
// leaves both writers disabled and records an append-only recovery event.
func RotateRecoveredIncarnations(ctx context.Context, dsn, confirmedDatabase string, requireTLS bool, args Rotation) (string, string, error) {
	conn, err := pgx.Connect(ctx, dsn)
	if err != nil {
		return "", "", err
	}
	defer conn.Close(context.Background())
	var database string
	var tls bool
	if err := conn.QueryRow(ctx, `SELECT current_database(),
		EXISTS(SELECT 1 FROM pg_stat_ssl WHERE pid=pg_backend_pid() AND ssl)`).Scan(&database, &tls); err != nil {
		return "", "", fmt.Errorf("state rotate incarnations: identity: %w", err)
	}
	if database != confirmedDatabase {
		return "", "", fmt.Errorf("state rotate incarnations: database %q differs from confirmation %q", database, confirmedDatabase)
	}
	if requireTLS && !tls {
		return "", "", errors.New("state rotate incarnations: TLS required")
	}
	var target, model string
	err = conn.QueryRow(ctx, `SELECT target_incarnation_id,model_incarnation_id
		FROM masi_rotate_recovered_incarnations($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)`,
		args.NewTargetIncarnation, args.NewModelIncarnation, args.Source,
		args.BackupDigest, args.SchemaDigest, args.SourceTimeline, args.RestoredTimeline,
		args.RecoveryID, args.Actor, args.TraceID).Scan(&target, &model)
	if err != nil {
		return "", "", fmt.Errorf("state rotate incarnations: %w", err)
	}
	return target, model, nil
}

// Rotation is one isolated PITR/restore/clone/rewind fence operation.
type Rotation struct {
	NewTargetIncarnation string
	NewModelIncarnation  string
	Source               string
	BackupDigest         string
	SchemaDigest         string
	SourceTimeline       int64
	RestoredTimeline     int64
	RecoveryID           string
	Actor                string
	TraceID              string
}
