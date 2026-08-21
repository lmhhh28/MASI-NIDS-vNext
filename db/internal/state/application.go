package state

import (
	"context"
	"errors"
	"fmt"
	"time"

	"github.com/jackc/pgx/v5"
)

// ApplicationInspection proves that the real application identity can use the
// pooled public SQL boundary while remaining outside maintenance/backup/private
// capabilities. The probe write is always rolled back.
type ApplicationInspection struct {
	Database                  string `json:"database"`
	User                      string `json:"user"`
	ServerVersionNum          int    `json:"server_version_num"`
	TLS                       bool   `json:"tls"`
	BackendPID                int32  `json:"backend_pid"`
	CanInsertRetentionHold    bool   `json:"can_insert_retention_hold"`
	CanDeleteRetentionHold    bool   `json:"can_delete_retention_hold"`
	CanUseAnalysisPrivate     bool   `json:"can_use_analysis_private"`
	IsBackupMember            bool   `json:"is_backup_member"`
	RolledBackProbeWrite      bool   `json:"rolled_back_probe_write"`
	PlaintextFallbackRejected bool   `json:"plaintext_fallback_rejected"`
}

// InspectApplication exercises the app login through PgBouncer using only the
// stable SQL contract. It does not require PgBouncer admin privileges.
func InspectApplication(ctx context.Context, dsn, confirmedDatabase string, requireTLS bool) (*ApplicationInspection, error) {
	conn, err := pgx.Connect(ctx, dsn)
	if err != nil {
		return nil, fmt.Errorf("application inspect: connect: %w", err)
	}
	defer conn.Close(context.Background())
	out := &ApplicationInspection{}
	if err := conn.QueryRow(ctx, `SELECT current_database(),current_user,
		current_setting('server_version_num')::int,
		EXISTS(SELECT 1 FROM pg_stat_ssl WHERE pid=pg_backend_pid() AND ssl),
		has_table_privilege(current_user,'retention_holds','INSERT'),
		has_table_privilege(current_user,'retention_holds','DELETE'),
		has_schema_privilege(current_user,'analysis_private','USAGE'),
		pg_has_role(current_user,'masi_backup','MEMBER')`).Scan(
		&out.Database, &out.User, &out.ServerVersionNum, &out.TLS,
		&out.CanInsertRetentionHold, &out.CanDeleteRetentionHold,
		&out.CanUseAnalysisPrivate, &out.IsBackupMember); err != nil {
		return nil, fmt.Errorf("application inspect: identity: %w", err)
	}
	if out.Database != confirmedDatabase || out.User != "masi_app_login" {
		return nil, errors.New("application inspect: unexpected database or identity")
	}
	if out.ServerVersionNum < 180000 || out.ServerVersionNum >= 190000 {
		return nil, errors.New("application inspect: PostgreSQL 18 required")
	}
	if requireTLS && !out.TLS {
		return nil, errors.New("application inspect: TLS required")
	}
	if !out.CanInsertRetentionHold || out.CanDeleteRetentionHold || out.CanUseAnalysisPrivate || out.IsBackupMember {
		return nil, errors.New("application inspect: least-privilege matrix mismatch")
	}
	tx, err := conn.Begin(ctx)
	if err != nil {
		return nil, err
	}
	probeID := fmt.Sprintf("module-probe-%d", time.Now().UnixNano())
	if err := tx.QueryRow(ctx, `SELECT pg_backend_pid()`).Scan(&out.BackendPID); err != nil {
		_ = tx.Rollback(context.Background())
		return nil, err
	}
	if _, err := tx.Exec(ctx, `INSERT INTO retention_holds(
		hold_id,fact_domain,scope,reason_code,starts_at,expires_at,actor_ref,trace_id
	) VALUES($1,'events','module-probe','qualification-probe',clock_timestamp(),
		clock_timestamp()+interval '1 minute','module-gate','module-gate')`, probeID); err != nil {
		_ = tx.Rollback(context.Background())
		return nil, fmt.Errorf("application inspect: bounded write: %w", err)
	}
	if err := tx.Rollback(ctx); err != nil {
		return nil, fmt.Errorf("application inspect: rollback: %w", err)
	}
	var exists bool
	if err := conn.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM retention_holds WHERE hold_id=$1)`, probeID).Scan(&exists); err != nil {
		return nil, err
	}
	out.RolledBackProbeWrite = !exists
	if !out.RolledBackProbeWrite {
		return nil, errors.New("application inspect: probe write escaped rollback")
	}
	if requireTLS {
		plainConfig, parseErr := pgx.ParseConfig(dsn)
		if parseErr != nil {
			return nil, parseErr
		}
		plainConfig.TLSConfig = nil
		plainConfig.Fallbacks = nil
		plainCtx, cancel := context.WithTimeout(ctx, 3*time.Second)
		plainConn, plainErr := pgx.ConnectConfig(plainCtx, plainConfig)
		cancel()
		if plainErr == nil {
			plainConn.Close(context.Background())
			return nil, errors.New("application inspect: plaintext fallback was accepted")
		}
		out.PlaintextFallbackRejected = true
	}
	return out, nil
}
