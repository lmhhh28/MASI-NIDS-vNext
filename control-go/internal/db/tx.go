package db

import (
	"context"
	"errors"
	"fmt"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgxpool"
)

// Tx is a short PostgreSQL transaction. The invariant enforced by this type:
// NO external call (Edge/P4/OIDC/MCP/A2A/HTTP/gRPC) may occur while a Tx is
// open. External work must happen AFTER Commit and BEFORE the CAS finalize
// re-opens a new short Tx. There is intentionally no method that yields the
// underlying connection to callers across an external wait.
type Tx struct {
	tx  pgx.Tx
	cfg txConfig
}

type txConfig struct {
	iso    pgx.TxIsoLevel
	access pgx.TxAccessMode
}

// TxOption configures a transaction.
type TxOption func(*txConfig)

// ReadCommitted is the default isolation for short read/CAS transactions
// (ADR-0004: short READ COMMITTED txn for CAS).
func ReadCommitted() TxOption {
	return func(c *txConfig) { c.iso = pgx.ReadCommitted }
}

// Serializable is used for claim transactions (effect-cas contract).
func Serializable() TxOption {
	return func(c *txConfig) { c.iso = pgx.Serializable }
}

// RepeatableRead provides one stable PostgreSQL snapshot for canonical input
// freeze plus durable run creation.
func RepeatableRead() TxOption { return func(c *txConfig) { c.iso = pgx.RepeatableRead } }

// ReadOnly marks a transaction read-only (preflight tokens, projections).
func ReadOnly() TxOption {
	return func(c *txConfig) { c.access = pgx.ReadOnly }
}

// Begin opens a short transaction. Callers MUST Commit or Rollback before any
// external call. Use WithTx for a scoped helper that always resolves the tx.
func (p *Pool) Begin(ctx context.Context, opts ...TxOption) (*Tx, error) {
	c := txConfig{iso: pgx.ReadCommitted, access: pgx.ReadWrite}
	for _, o := range opts {
		o(&c)
	}
	pgxOpts := pgx.TxOptions{IsoLevel: c.iso, AccessMode: c.access}
	pgxTx, err := p.Pool.BeginTx(ctx, pgxOpts)
	if err != nil {
		return nil, fmt.Errorf("db: begin: %w", err)
	}
	return &Tx{tx: pgxTx, cfg: c}, nil
}

// Exec runs a statement inside the transaction.
func (t *Tx) Exec(ctx context.Context, sql string, args ...any) (pgconn.CommandTag, error) {
	return t.tx.Exec(ctx, sql, args...)
}

// QueryRow runs a single-row query inside the transaction.
func (t *Tx) QueryRow(ctx context.Context, sql string, args ...any) pgx.Row {
	return t.tx.QueryRow(ctx, sql, args...)
}

// Query runs a multi-row query inside the transaction.
func (t *Tx) Query(ctx context.Context, sql string, args ...any) (pgx.Rows, error) {
	return t.tx.Query(ctx, sql, args...)
}

// Commit finalizes the transaction. After Commit, external calls are permitted.
func (t *Tx) Commit(ctx context.Context) error {
	if err := t.tx.Commit(ctx); err != nil {
		return fmt.Errorf("db: commit: %w", err)
	}
	return nil
}

// Rollback discards the transaction. Safe to call after Commit (returns
// pgx.ErrTxClosed, which WithTx ignores).
func (t *Tx) Rollback(ctx context.Context) error {
	return t.tx.Rollback(ctx)
}

// WithTx runs fn inside a short transaction. PostgreSQL serialization failures
// and deadlocks are retried at most three times with a small bounded backoff;
// the failed transaction is fully rolled back before fn is invoked again. A
// callback must therefore keep all durable side effects inside Tx (the package
// contract already forbids external waits/calls). The tx is ALWAYS resolved
// before WithTx returns.
func (p *Pool) WithTx(ctx context.Context, opts []TxOption, fn func(*Tx) error) error {
	const maxAttempts = 3
	var err error
	for attempt := 1; attempt <= maxAttempts; attempt++ {
		err = p.withTxOnce(ctx, opts, fn)
		if err == nil || !isRetryableTransactionError(err) || attempt == maxAttempts {
			return err
		}
		backoff := time.Duration(attempt*5) * time.Millisecond
		timer := time.NewTimer(backoff)
		select {
		case <-ctx.Done():
			timer.Stop()
			return errors.Join(err, ctx.Err())
		case <-timer.C:
		}
	}
	return err
}

func (p *Pool) withTxOnce(ctx context.Context, opts []TxOption, fn func(*Tx) error) (err error) {
	tx, err := p.Begin(ctx, opts...)
	if err != nil {
		return err
	}
	defer func() {
		if rbErr := tx.Rollback(context.Background()); rbErr != nil && !errors.Is(rbErr, pgx.ErrTxClosed) {
			err = fmt.Errorf("db: rollback: %v (orig %w)", rbErr, err)
		}
	}()
	if err = fn(tx); err != nil {
		return err
	}
	return tx.Commit(ctx)
}

func isRetryableTransactionError(err error) bool {
	var pgErr *pgconn.PgError
	if !errors.As(err, &pgErr) {
		return false
	}
	return pgErr.Code == "40001" || pgErr.Code == "40P01"
}

// AssertPoolNotNil guards against nil-pool use in tests.
func AssertPoolNotNil(p *Pool) error {
	if p == nil || p.Pool == nil {
		return errors.New("db: pool is nil")
	}
	return nil
}

// AcquireConn returns a connection from the pool for direct use (e.g. COPY).
// Callers MUST release it; it must not be held across an external wait.
func (p *Pool) AcquireConn(ctx context.Context) (*pgxpool.Conn, error) {
	return p.Pool.Acquire(ctx)
}
