package db

import (
	"errors"
	"testing"

	"github.com/jackc/pgx/v5/pgconn"
)

func TestRetryableTransactionErrorClosedSet(t *testing.T) {
	for _, code := range []string{"40001", "40P01"} {
		if !isRetryableTransactionError(&pgconn.PgError{Code: code}) {
			t.Fatalf("SQLSTATE %s must be retryable", code)
		}
	}
	if isRetryableTransactionError(&pgconn.PgError{Code: "23505"}) {
		t.Fatal("unique violation must not be blindly retried")
	}
	if isRetryableTransactionError(errors.New("serialization failure")) {
		t.Fatal("untyped text errors must not be retried")
	}
}
