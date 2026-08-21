package migrate

import (
	"context"
	"errors"
	"fmt"
	"io"
	"regexp"
	"time"

	"github.com/jackc/pgx/v5"
)

const (
	ProductionHistory = "masi_migration_history"
	TestHistory       = "masi_test_migration_history"
	defaultLockName   = "masi-nids-postgresql-state-migrations-v1"
)

var historyName = regexp.MustCompile(`^masi_(test_)?migration_history$`)

// Config freezes one explicit migration job. The application process never
// calls this package; deployment runs the dbctl binary as a one-shot job.
type Config struct {
	DSN                   string
	Directory             string
	ConfirmedDatabase     string
	RequireTestDatabase   bool
	RequireTLS            bool
	HistoryTable          string
	ExpectedSchemaVersion string
	SourceRevision        string
	AdvisoryLockName      string
	LockTimeout           time.Duration
	StatementTimeout      time.Duration
	Output                io.Writer
}

// Result is bounded, secret-free migration evidence.
type Result struct {
	Database         string   `json:"database"`
	ServerVersionNum int      `json:"server_version_num"`
	TLS              bool     `json:"tls"`
	SchemaVersion    string   `json:"schema_version"`
	ChainDigest      string   `json:"chain_digest"`
	HistoryTable     string   `json:"history_table"`
	Applied          []string `json:"applied"`
	AlreadyApplied   []string `json:"already_applied"`
	StartedAt        string   `json:"started_at"`
	CompletedAt      string   `json:"completed_at"`
	ElapsedMS        int64    `json:"elapsed_ms"`
}

// Run applies a contiguous immutable chain under one advisory lock. Every
// migration and its APPLIED history row share a transaction. Partial/interrupted
// statements, checksum drift, orphan history, wrong database, TLS downgrade,
// and unsupported PostgreSQL major fail closed.
func Run(ctx context.Context, cfg Config) (*Result, error) {
	started := time.Now().UTC()
	if cfg.DSN == "" || cfg.Directory == "" || cfg.ConfirmedDatabase == "" {
		return nil, errors.New("migrate: dsn file, directory, and exact confirmed database are required")
	}
	if !historyName.MatchString(cfg.HistoryTable) {
		return nil, errors.New("migrate: unsupported history table")
	}
	if cfg.ExpectedSchemaVersion == "" || cfg.SourceRevision == "" {
		return nil, errors.New("migrate: expected schema version and source revision are required")
	}
	if cfg.AdvisoryLockName == "" {
		cfg.AdvisoryLockName = defaultLockName
	}
	if cfg.LockTimeout <= 0 {
		cfg.LockTimeout = 5 * time.Second
	}
	if cfg.StatementTimeout <= 0 {
		cfg.StatementTimeout = 2 * time.Minute
	}
	if cfg.Output == nil {
		cfg.Output = io.Discard
	}
	files, chainDigest, err := LoadChain(cfg.Directory)
	if err != nil {
		return nil, err
	}
	conn, err := pgx.Connect(ctx, cfg.DSN)
	if err != nil {
		return nil, fmt.Errorf("migrate: connect: %w", err)
	}
	defer conn.Close(context.Background())

	result := &Result{ChainDigest: chainDigest, HistoryTable: cfg.HistoryTable,
		Applied: []string{}, AlreadyApplied: []string{}, StartedAt: started.Format(time.RFC3339Nano)}
	var tls bool
	if err := conn.QueryRow(ctx, `SELECT current_database(),current_setting('server_version_num')::int,
		EXISTS(SELECT 1 FROM pg_stat_ssl WHERE pid=pg_backend_pid() AND ssl)`).
		Scan(&result.Database, &result.ServerVersionNum, &tls); err != nil {
		return nil, fmt.Errorf("migrate: identify server: %w", err)
	}
	result.TLS = tls
	if result.Database != cfg.ConfirmedDatabase {
		return nil, fmt.Errorf("migrate: connected database %q differs from confirmation %q", result.Database, cfg.ConfirmedDatabase)
	}
	if cfg.RequireTestDatabase && !IsTestDatabase(result.Database) {
		return nil, fmt.Errorf("migrate: destructive target %q is not test-named", result.Database)
	}
	if result.ServerVersionNum < 180000 || result.ServerVersionNum >= 190000 {
		return nil, fmt.Errorf("migrate: PostgreSQL 18 required, got server_version_num=%d", result.ServerVersionNum)
	}
	if cfg.RequireTLS && !result.TLS {
		return nil, errors.New("migrate: TLS connection required; plaintext fallback rejected")
	}
	if _, err := conn.Exec(ctx, `SELECT pg_advisory_lock(hashtextextended($1,0))`, cfg.AdvisoryLockName); err != nil {
		return nil, fmt.Errorf("migrate: advisory lock: %w", err)
	}
	defer func() {
		_, _ = conn.Exec(context.Background(), `SELECT pg_advisory_unlock(hashtextextended($1,0))`, cfg.AdvisoryLockName)
	}()

	if err := ensureHistory(ctx, conn, cfg.HistoryTable); err != nil {
		return nil, err
	}
	known := make(map[string]File, len(files))
	for _, file := range files {
		known[file.Name] = file
	}
	rows, err := conn.Query(ctx, fmt.Sprintf(`SELECT name,checksum FROM %s ORDER BY version`, cfg.HistoryTable))
	if err != nil {
		return nil, fmt.Errorf("migrate: read history: %w", err)
	}
	for rows.Next() {
		var name, checksum string
		if err := rows.Scan(&name, &checksum); err != nil {
			rows.Close()
			return nil, fmt.Errorf("migrate: scan history: %w", err)
		}
		expected, found := known[name]
		if !found {
			rows.Close()
			return nil, fmt.Errorf("migrate: orphan history entry %q", name)
		}
		if checksum != expected.Checksum {
			rows.Close()
			return nil, fmt.Errorf("migrate: checksum drift for %q", name)
		}
	}
	if err := rows.Err(); err != nil {
		rows.Close()
		return nil, fmt.Errorf("migrate: history rows: %w", err)
	}
	rows.Close()

	for _, file := range files {
		var existing string
		err := conn.QueryRow(ctx, fmt.Sprintf(`SELECT checksum FROM %s WHERE name=$1`, cfg.HistoryTable), file.Name).Scan(&existing)
		if err == nil {
			if existing != file.Checksum {
				return nil, fmt.Errorf("migrate: checksum drift for %q", file.Name)
			}
			result.AlreadyApplied = append(result.AlreadyApplied, file.Name)
			continue
		}
		if !errors.Is(err, pgx.ErrNoRows) {
			return nil, fmt.Errorf("migrate: check %s: %w", file.Name, err)
		}
		tx, err := conn.Begin(ctx)
		if err != nil {
			return nil, fmt.Errorf("migrate: begin %s: %w", file.Name, err)
		}
		lockMS := cfg.LockTimeout.Milliseconds()
		statementMS := cfg.StatementTimeout.Milliseconds()
		if _, err := tx.Exec(ctx, fmt.Sprintf(`SET LOCAL lock_timeout='%dms'; SET LOCAL statement_timeout='%dms'`, lockMS, statementMS)); err != nil {
			_ = tx.Rollback(context.Background())
			return nil, fmt.Errorf("migrate: set deadlines %s: %w", file.Name, err)
		}
		if _, err := tx.Exec(ctx, file.Body); err != nil {
			_ = tx.Rollback(context.Background())
			return nil, fmt.Errorf("migrate: apply %s: %w", file.Name, err)
		}
		insert := fmt.Sprintf(`INSERT INTO %s(
			version,name,checksum,source_revision,source_digest,chain_digest,status,
			started_at,completed_at,applied_at)
			VALUES($1,$2,$3,$4,$5,$6,'APPLIED',clock_timestamp(),clock_timestamp(),clock_timestamp())`, cfg.HistoryTable)
		if _, err := tx.Exec(ctx, insert, file.Version, file.Name, file.Checksum,
			cfg.SourceRevision, file.Checksum, chainDigest); err != nil {
			_ = tx.Rollback(context.Background())
			return nil, fmt.Errorf("migrate: history %s: %w", file.Name, err)
		}
		if err := tx.Commit(ctx); err != nil {
			return nil, fmt.Errorf("migrate: commit %s: %w", file.Name, err)
		}
		result.Applied = append(result.Applied, file.Name)
		_, _ = fmt.Fprintf(cfg.Output, "applied %s %s\n", file.Name, file.Checksum)
	}
	if err := conn.QueryRow(ctx, `SELECT value FROM masi_schema_meta WHERE key='version'`).Scan(&result.SchemaVersion); err != nil {
		return nil, fmt.Errorf("migrate: final schema version: %w", err)
	}
	if result.SchemaVersion != cfg.ExpectedSchemaVersion {
		return nil, fmt.Errorf("migrate: schema version=%s expected=%s", result.SchemaVersion, cfg.ExpectedSchemaVersion)
	}
	if _, err := conn.Exec(ctx, `UPDATE masi_schema_meta
		SET checksum=$1,source_digest=$2,applied_at=clock_timestamp() WHERE key='version'`,
		chainDigest, "migration-chain/"+cfg.ExpectedSchemaVersion); err != nil {
		return nil, fmt.Errorf("migrate: publish chain digest: %w", err)
	}
	completed := time.Now().UTC()
	result.CompletedAt = completed.Format(time.RFC3339Nano)
	result.ElapsedMS = completed.Sub(started).Milliseconds()
	return result, nil
}

func ensureHistory(ctx context.Context, conn *pgx.Conn, table string) error {
	statements := []string{
		fmt.Sprintf(`CREATE TABLE IF NOT EXISTS %s(
			version INTEGER,
			name TEXT PRIMARY KEY,
			checksum TEXT NOT NULL CHECK(checksum ~ '^sha256:[0-9a-f]{64}$'),
			source_revision TEXT,
			source_digest TEXT,
			chain_digest TEXT,
			status TEXT NOT NULL DEFAULT 'APPLIED',
			started_at TIMESTAMPTZ,
			completed_at TIMESTAMPTZ,
			applied_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp())`, table),
		fmt.Sprintf(`ALTER TABLE %s ADD COLUMN IF NOT EXISTS version INTEGER,
			ADD COLUMN IF NOT EXISTS source_revision TEXT,
			ADD COLUMN IF NOT EXISTS source_digest TEXT,
			ADD COLUMN IF NOT EXISTS chain_digest TEXT,
			ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'APPLIED',
			ADD COLUMN IF NOT EXISTS started_at TIMESTAMPTZ,
			ADD COLUMN IF NOT EXISTS completed_at TIMESTAMPTZ`, table),
		fmt.Sprintf(`UPDATE %s SET version=substring(name from 1 for 4)::integer,
			source_revision=COALESCE(source_revision,'legacy:unknown'),
			source_digest=COALESCE(source_digest,checksum),
			chain_digest=COALESCE(chain_digest,checksum),
			started_at=COALESCE(started_at,applied_at),completed_at=COALESCE(completed_at,applied_at)
			WHERE version IS NULL OR source_revision IS NULL OR source_digest IS NULL
			   OR chain_digest IS NULL OR started_at IS NULL OR completed_at IS NULL`, table),
		fmt.Sprintf(`ALTER TABLE %s ALTER COLUMN version SET NOT NULL,
			ALTER COLUMN source_revision SET NOT NULL,ALTER COLUMN source_digest SET NOT NULL,
			ALTER COLUMN chain_digest SET NOT NULL,ALTER COLUMN started_at SET NOT NULL,
			ALTER COLUMN completed_at SET NOT NULL`, table),
		fmt.Sprintf(`CREATE UNIQUE INDEX IF NOT EXISTS %s_version_uidx ON %s(version)`, table, table),
	}
	for _, statement := range statements {
		if _, err := conn.Exec(ctx, statement); err != nil {
			return fmt.Errorf("migrate: prepare history: %w", err)
		}
	}
	return nil
}
