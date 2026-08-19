// Package db provides the PostgreSQL access foundation for Go Control Core:
// a bounded pgx pool, short-transaction helpers that enforce the no-external-
// wait invariant, CAS/idempotency/claim primitives (FOR UPDATE SKIP LOCKED),
// and a migration-aware reader (the application does NOT run migrations).
//
// Go is the sole business writer of 8 of 9 logical schema domains. The pool is
// role-separated; connections are bounded by the resource profile.
package db

import (
	"context"
	"fmt"

	"github.com/jackc/pgx/v5/pgxpool"

	"masi-nids/control-go/internal/config"
)

// Pool wraps a pgxpool.Pool with the bounded, role-separated configuration.
type Pool struct {
	*pgxpool.Pool
	cfg *config.Config
}

// New constructs and ping-validates the pool. The application does NOT run
// migrations (postgresql-state-design §5); schema compatibility is checked by
// MigrationReader at startup.
func New(ctx context.Context, cfg *config.Config) (*Pool, error) {
	pcfg, err := pgxpool.ParseConfig(cfg.PostgreSQLDSN)
	if err != nil {
		return nil, fmt.Errorf("db: parse dsn: %w", err)
	}
	if cfg.Resource.MaxPoolConnections > 0 {
		pcfg.MaxConns = int32(cfg.Resource.MaxPoolConnections)
	}
	if cfg.Resource.IdleConnTimeout > 0 {
		pcfg.MaxConnIdleTime = cfg.Resource.IdleConnTimeout
	}
	// Short transactions only: statement/lock timeouts bound every connection
	// so a stuck statement can never hold a connection indefinitely.
	if cfg.Resource.StatementTimeout > 0 {
		pcfg.ConnConfig.RuntimeParams["statement_timeout"] = fmt.Sprintf("%d", cfg.Resource.StatementTimeout.Milliseconds())
	}
	if cfg.Resource.LockTimeout > 0 {
		pcfg.ConnConfig.RuntimeParams["lock_timeout"] = fmt.Sprintf("%d", cfg.Resource.LockTimeout.Milliseconds())
	}
	pool, err := pgxpool.NewWithConfig(ctx, pcfg)
	if err != nil {
		return nil, fmt.Errorf("db: new pool: %w", err)
	}
	if err := pool.Ping(ctx); err != nil {
		pool.Close()
		return nil, fmt.Errorf("db: ping: %w", err)
	}
	if cfg.PostgreSQLRole != "" {
		var currentRole string
		if err := pool.QueryRow(ctx, `SELECT current_user`).Scan(&currentRole); err != nil {
			pool.Close()
			return nil, fmt.Errorf("db: verify current role: %w", err)
		}
		if currentRole != cfg.PostgreSQLRole {
			pool.Close()
			return nil, fmt.Errorf("db: current role %q != configured %q (fail closed)", currentRole, cfg.PostgreSQLRole)
		}
	}
	return &Pool{Pool: pool, cfg: cfg}, nil
}

// Close releases all pool connections.
func (p *Pool) Close() {
	if p.Pool != nil {
		p.Pool.Close()
	}
}
