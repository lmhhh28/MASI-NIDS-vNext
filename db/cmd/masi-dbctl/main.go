package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"os"
	"time"

	"masi-nids/postgresql-state/internal/migrate"
	"masi-nids/postgresql-state/internal/pool"
	"masi-nids/postgresql-state/internal/securefile"
	"masi-nids/postgresql-state/internal/state"
)

const usage = `masi-dbctl is the independent PostgreSQL State one-shot tool.

Commands:
  migrate              apply the immutable migration chain
  chain                validate and digest the migration chain without a DB
	  inspect              read back PostgreSQL/schema/role/partition state
	  inspect-pool         read back the exact PgBouncer transaction-pool profile
	  inspect-application  exercise the least-privilege pooled application boundary
	  inspect-roles        validate the sealed runtime login/group matrix
	  inspect-recovery     verify PITR target, writer fence and side-effect absence
	  inspect-replica      verify streaming/promotion marker and writer state
  wait-ready           bounded startup/readiness probe
  rotate-incarnations  fence a restored database before writers reopen

All commands take --dsn-file. Credentials in argv or environment are rejected.`

func main() {
	if err := run(os.Args[1:]); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}

func run(args []string) error {
	if len(args) == 0 {
		return errors.New("command required; run masi-dbctl help")
	}
	switch args[0] {
	case "migrate":
		return migrateCommand(args[1:])
	case "chain":
		return chainCommand(args[1:])
	case "inspect":
		return inspectCommand(args[1:])
	case "inspect-pool":
		return inspectPoolCommand(args[1:])
	case "inspect-application":
		return inspectApplicationCommand(args[1:])
	case "inspect-roles":
		return inspectRolesCommand(args[1:])
	case "inspect-recovery":
		return inspectRecoveryCommand(args[1:])
	case "inspect-replica":
		return inspectReplicaCommand(args[1:])
	case "wait-ready":
		return waitReadyCommand(args[1:])
	case "rotate-incarnations":
		return rotateCommand(args[1:])
	case "help", "--help", "-h":
		fmt.Println(usage)
		return nil
	default:
		return fmt.Errorf("unknown command %q\n%s", args[0], usage)
	}
}

func inspectReplicaCommand(args []string) error {
	set := flag.NewFlagSet("inspect-replica", flag.ContinueOnError)
	dsnFile := set.String("dsn-file", "", "standby/promoted PostgreSQL DSN secret file")
	database := set.String("confirm-database", "", "exact database name")
	marker := set.String("required-marker", "", "durable marker that must have replayed")
	requireTLS := set.Bool("require-tls", true, "require TLS")
	expectRecovery := set.Bool("expect-recovery", true, "expected pg_is_in_recovery value")
	if err := set.Parse(args); err != nil {
		return err
	}
	if set.NArg() != 0 {
		return errors.New("inspect-replica: unexpected positional arguments")
	}
	dsn, err := securefile.ReadSecret(*dsnFile)
	if err != nil {
		return err
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	result, err := state.InspectReplica(ctx, dsn, *database, *marker, *requireTLS, *expectRecovery)
	if err != nil {
		return err
	}
	return writeJSON(result)
}

func inspectRecoveryCommand(args []string) error {
	set := flag.NewFlagSet("inspect-recovery", flag.ContinueOnError)
	dsnFile := set.String("dsn-file", "", "restored PostgreSQL DSN secret file")
	database := set.String("confirm-database", "", "exact restored database name")
	requireTLS := set.Bool("require-tls", true, "require TLS")
	included := set.String("included-marker", "", "marker committed before restore point")
	excluded := set.String("excluded-marker", "", "marker committed after restore point")
	recoveryID := set.String("expected-recovery-id", "", "optional recovery event identity")
	if err := set.Parse(args); err != nil {
		return err
	}
	if set.NArg() != 0 {
		return errors.New("inspect-recovery: unexpected positional arguments")
	}
	dsn, err := securefile.ReadSecret(*dsnFile)
	if err != nil {
		return err
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	result, err := state.InspectRecovery(ctx, dsn, *database, *requireTLS, *included, *excluded, *recoveryID)
	if err != nil {
		return err
	}
	return writeJSON(result)
}

func inspectRolesCommand(args []string) error {
	set, dsnFile, database, requireTLS, err := commonFlags("inspect-roles", args)
	if err != nil {
		return err
	}
	if set.NArg() != 0 {
		return errors.New("inspect-roles: unexpected positional arguments")
	}
	dsn, err := securefile.ReadSecret(*dsnFile)
	if err != nil {
		return err
	}
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	result, err := state.InspectRuntimeRoles(ctx, dsn, *database, *requireTLS)
	if err != nil {
		return err
	}
	return writeJSON(result)
}

func inspectApplicationCommand(args []string) error {
	set, dsnFile, database, requireTLS, err := commonFlags("inspect-application", args)
	if err != nil {
		return err
	}
	if set.NArg() != 0 {
		return errors.New("inspect-application: unexpected positional arguments")
	}
	dsn, err := securefile.ReadSecret(*dsnFile)
	if err != nil {
		return err
	}
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	result, err := state.InspectApplication(ctx, dsn, *database, *requireTLS)
	if err != nil {
		return err
	}
	return writeJSON(result)
}

func inspectPoolCommand(args []string) error {
	set := flag.NewFlagSet("inspect-pool", flag.ContinueOnError)
	dsnFile := set.String("dsn-file", "", "PgBouncer admin DSN secret file")
	requireTLS := set.Bool("require-tls", true, "require the active PgBouncer connection to use TLS")
	if err := set.Parse(args); err != nil {
		return err
	}
	if set.NArg() != 0 {
		return errors.New("inspect-pool: unexpected positional arguments")
	}
	dsn, err := securefile.ReadSecret(*dsnFile)
	if err != nil {
		return err
	}
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	result, err := pool.Inspect(ctx, dsn, *requireTLS)
	if err != nil {
		return err
	}
	return writeJSON(result)
}

func chainCommand(args []string) error {
	set := flag.NewFlagSet("chain", flag.ContinueOnError)
	directory := set.String("directory", "/opt/masi/db/migrations", "immutable migration directory")
	if err := set.Parse(args); err != nil {
		return err
	}
	if set.NArg() != 0 {
		return errors.New("chain: unexpected positional arguments")
	}
	files, digest, err := migrate.LoadChain(*directory)
	if err != nil {
		return err
	}
	type item struct {
		Version  int    `json:"version"`
		Name     string `json:"name"`
		Checksum string `json:"checksum"`
	}
	items := make([]item, 0, len(files))
	for _, file := range files {
		items = append(items, item{Version: file.Version, Name: file.Name, Checksum: file.Checksum})
	}
	return writeJSON(map[string]any{"schema_version": "migration-chain-manifest/v1",
		"chain_digest": digest, "migration_count": len(items), "migrations": items})
}

func commonFlags(name string, args []string) (*flag.FlagSet, *string, *string, *bool, error) {
	set := flag.NewFlagSet(name, flag.ContinueOnError)
	dsnFile := set.String("dsn-file", "", "absolute path to one-line PostgreSQL DSN secret")
	database := set.String("confirm-database", "", "exact database name")
	requireTLS := set.Bool("require-tls", true, "require the active PostgreSQL connection to use TLS")
	if err := set.Parse(args); err != nil {
		return nil, nil, nil, nil, err
	}
	return set, dsnFile, database, requireTLS, nil
}

func migrateCommand(args []string) error {
	set := flag.NewFlagSet("migrate", flag.ContinueOnError)
	dsnFile := set.String("dsn-file", "", "PostgreSQL DSN secret file")
	directory := set.String("directory", "/opt/masi/db/migrations", "immutable migration directory")
	database := set.String("confirm-database", "", "exact database name")
	testOnly := set.Bool("require-test-database", false, "require database name containing test")
	requireTLS := set.Bool("require-tls", true, "require TLS")
	history := set.String("history-table", migrate.ProductionHistory, "migration history table")
	expectedVersion := set.String("expected-schema-version", "21", "schema API version")
	sourceRevision := set.String("source-revision", "unknown", "source revision bound to history")
	totalTimeout := set.Duration("total-timeout", 10*time.Minute, "total migration deadline")
	lockTimeout := set.Duration("lock-timeout", 5*time.Second, "per migration lock timeout")
	statementTimeout := set.Duration("statement-timeout", 2*time.Minute, "per migration statement timeout")
	if err := set.Parse(args); err != nil {
		return err
	}
	dsn, err := securefile.ReadSecret(*dsnFile)
	if err != nil {
		return err
	}
	ctx, cancel := context.WithTimeout(context.Background(), *totalTimeout)
	defer cancel()
	result, err := migrate.Run(ctx, migrate.Config{DSN: dsn, Directory: *directory,
		ConfirmedDatabase: *database, RequireTestDatabase: *testOnly, RequireTLS: *requireTLS,
		HistoryTable: *history, ExpectedSchemaVersion: *expectedVersion,
		SourceRevision: *sourceRevision, LockTimeout: *lockTimeout,
		StatementTimeout: *statementTimeout, Output: os.Stderr})
	if err != nil {
		return err
	}
	return writeJSON(result)
}

func inspectCommand(args []string) error {
	set, dsnFile, database, requireTLS, err := commonFlags("inspect", args)
	if err != nil {
		return err
	}
	if set.NArg() != 0 {
		return errors.New("inspect: unexpected positional arguments")
	}
	dsn, err := securefile.ReadSecret(*dsnFile)
	if err != nil {
		return err
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	result, err := state.Inspect(ctx, dsn, *database, migrate.ProductionHistory, *requireTLS)
	if err != nil {
		return err
	}
	return writeJSON(result)
}

func waitReadyCommand(args []string) error {
	set := flag.NewFlagSet("wait-ready", flag.ContinueOnError)
	dsnFile := set.String("dsn-file", "", "PostgreSQL DSN secret file")
	database := set.String("confirm-database", "", "exact database name")
	requireTLS := set.Bool("require-tls", true, "require TLS")
	timeout := set.Duration("timeout", 60*time.Second, "total readiness deadline")
	if err := set.Parse(args); err != nil {
		return err
	}
	dsn, err := securefile.ReadSecret(*dsnFile)
	if err != nil {
		return err
	}
	deadline := time.Now().Add(*timeout)
	var last error
	for attempt := 1; time.Now().Before(deadline); attempt++ {
		ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
		inspection, inspectErr := state.Inspect(ctx, dsn, *database, migrate.ProductionHistory, *requireTLS)
		cancel()
		if inspectErr == nil {
			return writeJSON(map[string]any{"ready": true, "attempt": attempt,
				"database": inspection.Database, "server_version_num": inspection.ServerVersionNum,
				"schema_chain_digest": inspection.SchemaChainDigest})
		}
		last = inspectErr
		time.Sleep(500 * time.Millisecond)
	}
	return fmt.Errorf("wait-ready: deadline exceeded: %w", last)
}

func rotateCommand(args []string) error {
	set := flag.NewFlagSet("rotate-incarnations", flag.ContinueOnError)
	dsnFile := set.String("dsn-file", "", "PostgreSQL DSN secret file")
	database := set.String("confirm-database", "", "exact restored database name")
	requireTLS := set.Bool("require-tls", true, "require TLS")
	target := set.String("new-target-incarnation", "", "never-used target-control incarnation")
	model := set.String("new-model-incarnation", "", "never-used model-control incarnation")
	source := set.String("source", "pitr", "pitr|restore|clone|rewind")
	backupDigest := set.String("backup-manifest-digest", "", "sha256 backup manifest digest")
	schemaDigest := set.String("schema-chain-digest", "", "sha256 migration chain digest")
	sourceTimeline := set.Int64("source-timeline", 0, "source PostgreSQL timeline")
	restoredTimeline := set.Int64("restored-timeline", 0, "restored PostgreSQL timeline")
	recoveryID := set.String("recovery-id", "", "stable recovery operation ID")
	actor := set.String("actor-ref", "", "maintenance actor reference")
	trace := set.String("trace-id", "", "trace reference")
	if err := set.Parse(args); err != nil {
		return err
	}
	dsn, err := securefile.ReadSecret(*dsnFile)
	if err != nil {
		return err
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	targetID, modelID, err := state.RotateRecoveredIncarnations(ctx, dsn, *database, *requireTLS, state.Rotation{
		NewTargetIncarnation: *target, NewModelIncarnation: *model, Source: *source,
		BackupDigest: *backupDigest, SchemaDigest: *schemaDigest,
		SourceTimeline: *sourceTimeline, RestoredTimeline: *restoredTimeline,
		RecoveryID: *recoveryID, Actor: *actor, TraceID: *trace})
	if err != nil {
		return err
	}
	return writeJSON(map[string]any{"target_control_incarnation_id": targetID,
		"model_control_incarnation_id": modelID, "writers_enabled": false})
}

func writeJSON(value any) error {
	encoder := json.NewEncoder(os.Stdout)
	encoder.SetEscapeHTML(true)
	encoder.SetIndent("", "  ")
	return encoder.Encode(value)
}
