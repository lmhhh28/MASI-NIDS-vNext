package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"os"
	"time"

	"masi-nids/postgresql-state/internal/securefile"
	"masi-nids/postgresql-state/internal/workload"
)

func main() {
	if err := run(os.Args[1:]); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}

func run(args []string) error {
	set := flag.NewFlagSet("masi-dbload", flag.ContinueOnError)
	dsnFile := set.String("dsn-file", "", "mode-0600 PgBouncer application DSN file")
	database := set.String("confirm-database", "", "exact test database")
	mode := set.String("mode", "capacity", "capacity|soak")
	requireTLS := set.Bool("require-tls", true, "require TLS")
	formal := set.Bool("formal", false, "require the exact 4x900 second soak")
	phaseDuration := set.Duration("phase-duration", 10*time.Second, "rehearsal phase duration")
	sampleEvery := set.Duration("sample-every", 10*time.Second, "bounded evidence sample interval")
	warmup := set.Duration("warmup", 0, "bounded warmup duration")
	if err := set.Parse(args); err != nil {
		return err
	}
	if set.NArg() != 0 || *database == "" {
		return errors.New("masi-dbload: exact database and no positional arguments required")
	}
	dsn, err := securefile.ReadSecret(*dsnFile)
	if err != nil {
		return err
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Hour)
	defer cancel()
	pool, identity, err := workload.Open(ctx, dsn, *database, "masi_app_login", *requireTLS, 32)
	if err != nil {
		return err
	}
	defer pool.Close()
	output := map[string]any{"schema_version": "postgresql-state-workload/v1", "mode": *mode,
		"identity": identity, "phases": workload.Phases()}
	switch *mode {
	case "capacity":
		measurements, runErr := workload.RunCapacity(ctx, pool, []int{0, 128, 1024, 4096})
		if runErr != nil {
			return runErr
		}
		output["capacity"] = measurements
		output["result"] = "PASS"
	case "soak":
		if *formal {
			*phaseDuration = 900 * time.Second
			*warmup = 60 * time.Second
		}
		result, runErr := workload.RunSoak(ctx, pool, workload.SoakConfig{PhaseDuration: *phaseDuration,
			SampleEvery: *sampleEvery, Warmup: *warmup, Formal: *formal}, func(sample workload.SoakSample) {
			fmt.Fprintf(os.Stderr, "phase=%s operations=%d errors=%d p99_upper_ms=%d pool=%d/%d\n",
				sample.Phase, sample.Operations, sample.Errors, sample.P99UpperBoundMS,
				sample.PoolAcquired, sample.PoolTotal)
		})
		output["soak"] = result
		if runErr != nil {
			output["result"] = "FAIL"
			_ = writeJSON(output)
			return runErr
		}
		output["result"] = "PASS"
	default:
		return errors.New("masi-dbload: unknown mode")
	}
	return writeJSON(output)
}

func writeJSON(value any) error {
	encoder := json.NewEncoder(os.Stdout)
	encoder.SetIndent("", "  ")
	return encoder.Encode(value)
}
