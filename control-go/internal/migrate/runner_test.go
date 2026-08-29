package migrate

import (
	"context"
	"strings"
	"testing"
)

func TestMigrationBody(t *testing.T) {
	body, err := MigrationBody([]byte("-- x\nBEGIN;\nSELECT 1;\nCOMMIT;\n"))
	if err != nil || body == "" {
		t.Fatalf("valid envelope: %q %v", body, err)
	}
	if _, err := MigrationBody([]byte("SELECT 1;")); err == nil {
		t.Fatal("missing envelope must fail")
	}
	if _, err := MigrationBody([]byte("BEGIN;\nBEGIN;\nCOMMIT;")); err == nil {
		t.Fatal("nested envelope must fail")
	}
}

func TestRunnerCannotTargetProductionHistory(t *testing.T) {
	err := Run(context.Background(), Config{
		DSN:                 "postgres://invalid",
		Directory:           t.TempDir(),
		ConfirmedDatabase:   "masi_state",
		RequireTestDatabase: false,
		HistoryTable:        "masi_migration_history",
		AdvisoryLockName:    "forbidden-production-run",
	})
	if err == nil || !strings.Contains(err.Error(), "test-only runner") {
		t.Fatalf("production-capable migration configuration was not rejected: %v", err)
	}
}
