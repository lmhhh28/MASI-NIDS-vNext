package state

import (
	"context"
	"errors"
	"fmt"

	"github.com/jackc/pgx/v5"
)

type RecoveryInspection struct {
	Database                  string `json:"database"`
	TLS                       bool   `json:"tls"`
	InRecovery                bool   `json:"in_recovery"`
	TimelineID                int64  `json:"timeline_id"`
	IncludedMarkerPresent     bool   `json:"included_marker_present"`
	ExcludedMarkerAbsent      bool   `json:"excluded_marker_absent"`
	TargetWriterEnabled       bool   `json:"target_writer_enabled"`
	ModelWriterEnabled        bool   `json:"model_writer_enabled"`
	TargetControlIncarnation  string `json:"target_control_incarnation"`
	ModelControlIncarnation   string `json:"model_control_incarnation"`
	EffectIntentCount         int64  `json:"effect_intent_count"`
	PluginStatisticRunCount   int64  `json:"plugin_statistic_run_count"`
	RecoveryEventPresent      bool   `json:"recovery_event_present"`
	RecoveryEventIsolated     bool   `json:"recovery_event_isolated"`
	RecoveryEventWriterOpened bool   `json:"recovery_event_writer_opened"`
}

// InspectRecovery checks the named-point inclusion/exclusion oracle and proves
// that an isolated restore did not open writers or create durable work.
func InspectRecovery(ctx context.Context, dsn, confirmedDatabase string, requireTLS bool,
	includedMarker, excludedMarker, expectedRecoveryID string) (*RecoveryInspection, error) {
	if includedMarker == "" || excludedMarker == "" || includedMarker == excludedMarker {
		return nil, errors.New("recovery inspect: distinct include/exclude markers required")
	}
	conn, err := pgx.Connect(ctx, dsn)
	if err != nil {
		return nil, fmt.Errorf("recovery inspect: connect: %w", err)
	}
	defer conn.Close(context.Background())
	out := &RecoveryInspection{}
	if err := conn.QueryRow(ctx, `SELECT current_database(),
		EXISTS(SELECT 1 FROM pg_stat_ssl WHERE pid=pg_backend_pid() AND ssl),
		pg_is_in_recovery(),CASE WHEN pg_is_in_recovery()
		THEN COALESCE((pg_control_checkpoint()).timeline_id,1)
		ELSE (('x'||substring(pg_walfile_name(pg_current_wal_lsn()),1,8))::bit(32)::bigint) END,
		EXISTS(SELECT 1 FROM retention_holds WHERE hold_id=$1),
		NOT EXISTS(SELECT 1 FROM retention_holds WHERE hold_id=$2),
		t.writer_enabled,m.writer_enabled,COALESCE(t.active_incarnation_id,''),
		COALESCE(m.active_incarnation_id,''),(SELECT count(*) FROM effect_intents),
		(SELECT count(*) FROM plugin_statistic_runs)
	FROM target_control_state t CROSS JOIN model_control_state m
	WHERE t.singleton AND m.singleton`, includedMarker, excludedMarker).Scan(
		&out.Database, &out.TLS, &out.InRecovery, &out.TimelineID,
		&out.IncludedMarkerPresent, &out.ExcludedMarkerAbsent,
		&out.TargetWriterEnabled, &out.ModelWriterEnabled,
		&out.TargetControlIncarnation, &out.ModelControlIncarnation,
		&out.EffectIntentCount, &out.PluginStatisticRunCount); err != nil {
		return nil, fmt.Errorf("recovery inspect: state: %w", err)
	}
	if out.Database != confirmedDatabase || (requireTLS && !out.TLS) || out.InRecovery ||
		!out.IncludedMarkerPresent || !out.ExcludedMarkerAbsent ||
		out.TargetWriterEnabled || out.ModelWriterEnabled || out.EffectIntentCount != 0 ||
		out.PluginStatisticRunCount != 0 {
		return nil, errors.New("recovery inspect: target, writer or side-effect oracle mismatch")
	}
	if expectedRecoveryID != "" {
		if err := conn.QueryRow(ctx, `SELECT true,isolated,writer_opened
			FROM database_recovery_events WHERE recovery_id=$1`, expectedRecoveryID).Scan(
			&out.RecoveryEventPresent, &out.RecoveryEventIsolated,
			&out.RecoveryEventWriterOpened); err != nil {
			return nil, fmt.Errorf("recovery inspect: recovery event: %w", err)
		}
		if !out.RecoveryEventPresent || !out.RecoveryEventIsolated || out.RecoveryEventWriterOpened {
			return nil, errors.New("recovery inspect: invalid recovery event")
		}
	}
	return out, nil
}
