// Command migrate-test is the isolated migration runner used by MOD-CTRL-001
// process E2E. It refuses every database whose current_database() does not
// contain "test". Production migrations remain the responsibility of MOD-DB-001.
package main

import (
	"context"
	"flag"
	"fmt"
	"os"
	"time"

	"masi-nids/control-go/internal/migrate"
)

func main() {
	if err := run(); err != nil {
		_, _ = fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}

func run() error {
	dsn := flag.String("dsn", "", "PostgreSQL test DSN")
	dir := flag.String("dir", "../db/migrations", "migration directory")
	confirm := flag.String("confirm-database", "masi_control_test", "exact test database name")
	flag.Parse()
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Minute)
	defer cancel()
	return migrate.Run(ctx, migrate.Config{
		DSN: *dsn, Directory: *dir, ConfirmedDatabase: *confirm,
		RequireTestDatabase: true, HistoryTable: migrate.TestHistory,
		AdvisoryLockName: "masi-control-migrate-test", Output: os.Stdout,
	})
}

func migrationBody(raw []byte) (string, error) {
	return migrate.MigrationBody(raw)
}
