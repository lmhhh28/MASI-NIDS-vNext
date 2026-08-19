package db

import (
	"context"
	"fmt"
	"path/filepath"
	"strconv"
	"strings"

	"github.com/jackc/pgx/v5"
)

// MigrationReader checks schema compatibility at startup. The application does
// NOT run migrations (postgresql-state-design §5: migrations run by an
// independent job/binary). Go authors the v1 schema SQL under db/migrations/;
// the db/ module (MOD-DB-001) later adopts it into the canonical migration
// runner/HA chain (plan decision D1).
//
// On incompatible schema (wrong version/digest) the reader fails closed.
type MigrationReader struct {
	pool           *Pool
	expectedVer    int
	expectedDigest string
}

// NewMigrationReader constructs a reader expecting the given schema version.
func NewMigrationReader(pool *Pool, expectedVersion int, expectedDigest string) *MigrationReader {
	return &MigrationReader{pool: pool, expectedVer: expectedVersion, expectedDigest: expectedDigest}
}

// CheckSchemaVersion verifies the live schema version matches the expected
// frozen version. Unknown/wrong version -> fail closed (no silent operation on
// an incompatible schema).
func (m *MigrationReader) CheckSchemaVersion(ctx context.Context) error {
	if m.pool == nil || m.pool.Pool == nil {
		return fmt.Errorf("migration_reader: pool is nil")
	}
	// masi_schema_meta.value is a TEXT column (generic key/value meta), so the
	// version is scanned as a string and parsed to int. pgx v5 does not coerce a
	// TEXT column into *int; scanning a string + strconv.Atoi keeps the schema
	// as-authored and fails closed on a non-integer value.
	var versionRaw string
	err := m.pool.QueryRow(ctx, `SELECT value FROM masi_schema_meta WHERE key = 'version'`).Scan(&versionRaw)
	if err != nil {
		if isNoRows(err) {
			return fmt.Errorf("migration_reader: schema version not recorded (unmigrated DB -> fail closed)")
		}
		return fmt.Errorf("migration_reader: read version: %w", err)
	}
	version, parseErr := strconv.Atoi(versionRaw)
	if parseErr != nil {
		return fmt.Errorf("migration_reader: schema version not an integer %q (fail closed): %w", versionRaw, parseErr)
	}
	if version != m.expectedVer {
		return fmt.Errorf("migration_reader: schema version %d != expected %d (fail closed)", version, m.expectedVer)
	}
	var schemaDigest string
	if err := m.pool.QueryRow(ctx, `SELECT checksum FROM masi_schema_meta WHERE key='version'`).Scan(&schemaDigest); err != nil {
		return fmt.Errorf("migration_reader: read schema digest: %w", err)
	}
	if schemaDigest != m.expectedDigest {
		return fmt.Errorf("migration_reader: schema digest %s != expected %s (fail closed)", schemaDigest, m.expectedDigest)
	}
	// The version row alone does not prove the schema is fully applied: 0001
	// records version=1 inside its own transaction, so a later migration that
	// fails to apply can leave version=1 with missing tables. The canonical
	// version/checksum integrity is owned by the migration runner (MOD-DB-001);
	// this defense-in-depth check makes the Go app fail closed on a partial
	// own-schema regardless, satisfying the fail-closed Module-Complete gate
	// before MOD-DB-001 exists.
	if err := m.checkRequiredTables(ctx); err != nil {
		return err
	}
	return nil
}

// v1RequiredTables are the anchor tables (one per migration domain) that must
// exist for the v1 schema to be considered fully applied. MOD-DB-001 will later
// own canonical checksum/version integrity; this list is the Go app's
// complementary startup safety net.
var v1RequiredTables = []string{
	"events", "effect_intents", "firewall_revisions", "targets",
	"shard_bindings", "plugin_manifests", "rule_observations",
	"plugin_statistics_current", "model_pool_observation_events",
	"incident_projection_events",
	"effect_acknowledgements",
	"target_lifecycle_events",
	"model_rollout_groups",
	"event_identities",
	"api_mutation_idempotency",
}

// checkRequiredTables verifies every v1 anchor table exists; a missing table
// means a partially-applied schema and fails closed at startup.
func (m *MigrationReader) checkRequiredTables(ctx context.Context) error {
	for _, t := range v1RequiredTables {
		var exists bool
		err := m.pool.QueryRow(ctx,
			`SELECT EXISTS (SELECT 1 FROM pg_tables WHERE schemaname = 'public' AND tablename = $1)`,
			t).Scan(&exists)
		if err != nil {
			return fmt.Errorf("migration_reader: verify table %q: %w", t, err)
		}
		if !exists {
			return fmt.Errorf("migration_reader: required table %q missing (partial schema -> fail closed)", t)
		}
	}
	return nil
}

// ExpectedVersion returns the frozen expected schema version.
func (m *MigrationReader) ExpectedVersion() int { return m.expectedVer }

func isNoRows(err error) bool { return isNoRowsPgx(err) }

func isNoRowsPgx(err error) bool {
	return strings.Contains(err.Error(), "no rows") || err == pgx.ErrNoRows
}

// MigrationFile represents one authored migration with its version, checksum,
// and source digest (immutable; partial/interrupted/retry/checksum drift must
// be detectable — postgresql-state-design §5).
type MigrationFile struct {
	Version      int    `json:"version"`
	Name         string `json:"name"`
	Checksum     string `json:"checksum"`
	SourceDigest string `json:"source_digest"`
}

// IsTestDBName confirms a database name is test-named (destructive tests only
// on a DB whose name contains "test"). This guards the integration test runner.
func IsTestDBName(name string) bool {
	return strings.Contains(strings.ToLower(name), "test")
}

// SafeMigrationPath rejects path traversal in migration file paths.
func SafeMigrationPath(base, name string) (string, error) {
	absBase, err := filepath.Abs(base)
	if err != nil {
		return "", fmt.Errorf("migration_reader: resolve base: %w", err)
	}
	candidate, err := filepath.Abs(filepath.Join(absBase, name))
	if err != nil {
		return "", fmt.Errorf("migration_reader: resolve path: %w", err)
	}
	rel, err := filepath.Rel(absBase, candidate)
	if err != nil {
		return "", fmt.Errorf("migration_reader: compare path: %w", err)
	}
	if rel == ".." || strings.HasPrefix(rel, ".."+string(filepath.Separator)) || filepath.IsAbs(rel) {
		return "", fmt.Errorf("migration_reader: path escapes base: %s", name)
	}
	return candidate, nil
}
