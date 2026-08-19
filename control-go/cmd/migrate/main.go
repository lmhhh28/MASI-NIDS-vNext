// Command migrate is the explicit, independent production migration job. The
// control-core application never invokes it. Operators must confirm the exact
// connected database name; checksum drift and non-contiguous chains fail closed.
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
	dsn := flag.String("dsn", "", "PostgreSQL migration-job DSN (session connection; do not use PgBouncer transaction pooling)")
	dir := flag.String("dir", "../db/migrations", "immutable migration directory")
	confirm := flag.String("confirm-database", "", "exact current_database() value required before any schema write")
	timeout := flag.Duration("timeout", 10*time.Minute, "bounded total migration deadline")
	flag.Parse()
	if *timeout <= 0 || *timeout > time.Hour {
		return fmt.Errorf("migrate: timeout must be >0 and <=1h")
	}
	ctx, cancel := context.WithTimeout(context.Background(), *timeout)
	defer cancel()
	return migrate.Run(ctx, migrate.Config{
		DSN: *dsn, Directory: *dir, ConfirmedDatabase: *confirm,
		HistoryTable: migrate.ProductionHistory, AdvisoryLockName: "masi-control-migrate-production",
		Output: os.Stdout,
	})
}
