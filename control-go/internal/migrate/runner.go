// Package migrate is the test-only PostgreSQL migration harness used by
// MOD-CTRL-001 process tests. Production schema changes are owned by the
// independent db/masi-dbctl job; neither Control Core nor its OCI image exposes
// a migration writer.
package migrate

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"

	"github.com/jackc/pgx/v5"

	"masi-nids/control-go/internal/db"
)

const (
	ExpectedSchemaVersion = "22"
	SchemaSource          = "migration-chain/v22"
	TestHistory           = "masi_test_migration_history"
)

// Config freezes the safety and evidence policy for one migration run.
type Config struct {
	DSN                 string
	Directory           string
	ConfirmedDatabase   string
	RequireTestDatabase bool
	HistoryTable        string
	AdvisoryLockName    string
	Output              io.Writer
}

type file struct {
	name     string
	checksum string
	body     string
}

// Run validates and applies the contiguous migration chain under a PostgreSQL
// advisory lock. Each file and its history row commit atomically. Existing
// history is immutable: checksum drift and orphaned history fail closed.
func Run(ctx context.Context, cfg Config) error {
	if cfg.DSN == "" || cfg.Directory == "" || cfg.ConfirmedDatabase == "" {
		return errors.New("migrate: dsn, directory, and exact confirmed database are required")
	}
	if !cfg.RequireTestDatabase || cfg.HistoryTable != TestHistory {
		return errors.New("migrate: test-only runner requires test database and test history")
	}
	if cfg.AdvisoryLockName == "" {
		return errors.New("migrate: advisory lock name required")
	}
	if cfg.Output == nil {
		cfg.Output = io.Discard
	}
	files, chainDigest, err := loadChain(cfg.Directory)
	if err != nil {
		return err
	}
	conn, err := pgx.Connect(ctx, cfg.DSN)
	if err != nil {
		return fmt.Errorf("migrate: connect: %w", err)
	}
	defer conn.Close(context.Background())

	var database string
	if err := conn.QueryRow(ctx, `SELECT current_database()`).Scan(&database); err != nil {
		return fmt.Errorf("migrate: identify database: %w", err)
	}
	if database != cfg.ConfirmedDatabase {
		return fmt.Errorf("migrate: connected database %q does not equal --confirm-database %q", database, cfg.ConfirmedDatabase)
	}
	if !db.IsTestDBName(database) {
		return fmt.Errorf("migrate: database %q is not test-named", database)
	}
	if _, err := conn.Exec(ctx, `SELECT pg_advisory_lock(hashtext($1))`, cfg.AdvisoryLockName); err != nil {
		return fmt.Errorf("migrate: acquire advisory lock: %w", err)
	}
	defer func() {
		_, _ = conn.Exec(context.Background(), `SELECT pg_advisory_unlock(hashtext($1))`, cfg.AdvisoryLockName)
	}()

	createHistory := fmt.Sprintf(`CREATE TABLE IF NOT EXISTS %s(
		name TEXT PRIMARY KEY,
			checksum TEXT NOT NULL CHECK (checksum ~ '^sha256:[0-9a-f]{64}$'
			 AND checksum <> ('sha256:' || repeat('0',64))),
		applied_at TIMESTAMPTZ NOT NULL DEFAULT now())`, cfg.HistoryTable)
	if _, err := conn.Exec(ctx, createHistory); err != nil {
		return fmt.Errorf("migrate: create history: %w", err)
	}
	known := make(map[string]string, len(files))
	for _, f := range files {
		known[f.name] = f.checksum
	}
	rows, err := conn.Query(ctx, fmt.Sprintf(`SELECT name,checksum FROM %s ORDER BY name`, cfg.HistoryTable))
	if err != nil {
		return fmt.Errorf("migrate: read history: %w", err)
	}
	for rows.Next() {
		var name, checksum string
		if err := rows.Scan(&name, &checksum); err != nil {
			rows.Close()
			return err
		}
		expected, ok := known[name]
		if !ok {
			rows.Close()
			return fmt.Errorf("migrate: orphaned history entry %s", name)
		}
		if checksum != expected {
			rows.Close()
			return fmt.Errorf("migrate: checksum drift for %s", name)
		}
	}
	if err := rows.Err(); err != nil {
		rows.Close()
		return err
	}
	rows.Close()

	for _, f := range files {
		var existing string
		err := conn.QueryRow(ctx, fmt.Sprintf(`SELECT checksum FROM %s WHERE name=$1`, cfg.HistoryTable), f.name).Scan(&existing)
		if err == nil {
			if existing != f.checksum {
				return fmt.Errorf("migrate: checksum drift for %s", f.name)
			}
			continue
		}
		if !errors.Is(err, pgx.ErrNoRows) {
			return err
		}
		tx, err := conn.Begin(ctx)
		if err != nil {
			return err
		}
		if _, err := tx.Exec(ctx, f.body); err != nil {
			_ = tx.Rollback(context.Background())
			return fmt.Errorf("migrate: apply %s: %w", f.name, err)
		}
		insertHistory := fmt.Sprintf(`INSERT INTO %s(name,checksum) VALUES($1,$2)`, cfg.HistoryTable)
		if _, err := tx.Exec(ctx, insertHistory, f.name, f.checksum); err != nil {
			_ = tx.Rollback(context.Background())
			return fmt.Errorf("migrate: record %s: %w", f.name, err)
		}
		if err := tx.Commit(ctx); err != nil {
			return fmt.Errorf("migrate: commit %s: %w", f.name, err)
		}
		_, _ = fmt.Fprintln(cfg.Output, "applied", f.name, f.checksum)
	}

	var schemaVersion string
	if err := conn.QueryRow(ctx, `SELECT value FROM masi_schema_meta WHERE key='version'`).Scan(&schemaVersion); err != nil {
		return fmt.Errorf("migrate: read final schema version: %w", err)
	}
	if schemaVersion != ExpectedSchemaVersion {
		return fmt.Errorf("migrate: final schema version %s, want %s", schemaVersion, ExpectedSchemaVersion)
	}
	if _, err := conn.Exec(ctx, `UPDATE masi_schema_meta SET checksum=$1,source_digest=$2,applied_at=now() WHERE key='version'`, chainDigest, SchemaSource); err != nil {
		return fmt.Errorf("migrate: record chain digest: %w", err)
	}
	_, _ = fmt.Fprintln(cfg.Output, "schema", chainDigest)
	return nil
}

func loadChain(directory string) ([]file, string, error) {
	entries, err := os.ReadDir(directory)
	if err != nil {
		return nil, "", fmt.Errorf("migrate: read directory: %w", err)
	}
	names := make([]string, 0, len(entries))
	for _, entry := range entries {
		if !entry.IsDir() && strings.HasSuffix(entry.Name(), ".sql") {
			names = append(names, entry.Name())
		}
	}
	sort.Strings(names)
	if len(names) == 0 {
		return nil, "", errors.New("migrate: no migration files")
	}
	chain := sha256.New()
	files := make([]file, 0, len(names))
	for i, name := range names {
		if len(name) < 6 || name[4] != '_' {
			return nil, "", fmt.Errorf("migrate: malformed migration name %s", name)
		}
		version, err := strconv.Atoi(name[:4])
		if err != nil || version != i+1 {
			return nil, "", fmt.Errorf("migrate: migrations must be contiguous from 0001 (got %s)", name)
		}
		path, err := db.SafeMigrationPath(directory, name)
		if err != nil {
			return nil, "", err
		}
		raw, err := os.ReadFile(filepath.Clean(path))
		if err != nil {
			return nil, "", err
		}
		sum := sha256.Sum256(raw)
		checksum := "sha256:" + hex.EncodeToString(sum[:])
		_, _ = fmt.Fprintf(chain, "%s\t%s\n", name, checksum)
		body, err := MigrationBody(raw)
		if err != nil {
			return nil, "", fmt.Errorf("migrate: %s: %w", name, err)
		}
		files = append(files, file{name: name, checksum: checksum, body: body})
	}
	return files, "sha256:" + hex.EncodeToString(chain.Sum(nil)), nil
}

// MigrationBody strips the one required outer BEGIN/COMMIT envelope because
// the runner owns the atomic transaction and history write.
func MigrationBody(raw []byte) (string, error) {
	lines := strings.Split(string(raw), "\n")
	begin, last := -1, -1
	for i, line := range lines {
		if strings.EqualFold(strings.TrimSpace(line), "BEGIN;") {
			begin = i
			break
		}
	}
	for i := len(lines) - 1; i >= 0; i-- {
		if strings.EqualFold(strings.TrimSpace(lines[i]), "COMMIT;") {
			last = i
			break
		}
	}
	if begin < 0 || last <= begin {
		return "", errors.New("migration must have one outer BEGIN/COMMIT envelope")
	}
	for i, line := range lines {
		trimmed := strings.ToUpper(strings.TrimSpace(line))
		if (trimmed == "BEGIN;" && i != begin) || (trimmed == "COMMIT;" && i != last) {
			return "", errors.New("nested transaction envelope rejected")
		}
	}
	lines[begin], lines[last] = "", ""
	return strings.Join(lines, "\n"), nil
}
