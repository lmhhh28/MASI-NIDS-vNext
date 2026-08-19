package api

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"time"

	"masi-nids/control-go/internal/db"
	"masi-nids/control-go/internal/security"
)

const maxMutationResponseBytes = 2 * 1024 * 1024

type idempotencyDisposition int

const (
	idempotencyExecute idempotencyDisposition = iota
	idempotencyReplay
	idempotencyConflict
	idempotencyInProgress
)

type storedMutationResponse struct {
	Status int
	Body   []byte
}

func requireMutationIdempotency(pool *db.Pool) func(http.Handler) http.Handler {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			if pool == nil || pool.Pool == nil {
				WriteError(w, http.StatusServiceUnavailable, "IDEMPOTENCY_STORE_UNAVAILABLE")
				return
			}
			session, err := sessionFromContext(r.Context())
			if err != nil {
				WriteError(w, http.StatusUnauthorized, "UNAUTHENTICATED")
				return
			}
			raw, err := io.ReadAll(http.MaxBytesReader(w, r.Body, 2*1024*1024))
			if err != nil {
				WriteError(w, http.StatusRequestEntityTooLarge, "MUTATION_BODY_TOO_LARGE")
				return
			}
			key, digest, err := canonicalMutationIdentity(raw)
			if err != nil {
				WriteError(w, http.StatusBadRequest, "IDEMPOTENCY_KEY_REQUIRED")
				return
			}
			r.Body = io.NopCloser(bytes.NewReader(raw))
			actor := security.Actor{Issuer: session.Actor.Issuer, Subject: session.Actor.Subject}
			disposition, replay, err := beginMutation(r.Context(), pool, actor, key, digest)
			if err != nil {
				WriteError(w, http.StatusServiceUnavailable, "IDEMPOTENCY_BEGIN_FAILED")
				return
			}
			switch disposition {
			case idempotencyReplay:
				w.Header().Set("Content-Type", "application/json")
				w.Header().Set("Cache-Control", "no-store")
				w.Header().Set("Idempotency-Replayed", "true")
				w.WriteHeader(replay.Status)
				_, _ = w.Write(replay.Body)
				return
			case idempotencyConflict:
				WriteError(w, http.StatusConflict, "IDEMPOTENCY_KEY_CONFLICT")
				return
			case idempotencyInProgress:
				WriteError(w, http.StatusConflict, "IDEMPOTENCY_RECONCILE_REQUIRED")
				return
			}

			buffer := newBufferedResponse()
			next.ServeHTTP(buffer, r)
			status, body := buffer.result()
			if err := completeMutation(r.Context(), pool, actor, key, digest, status, body); err != nil {
				WriteError(w, http.StatusServiceUnavailable, "IDEMPOTENCY_FINALIZE_UNKNOWN")
				return
			}
			copyHeaders(w.Header(), buffer.Header())
			w.WriteHeader(status)
			_, _ = w.Write(body)
		})
	}
}

func canonicalMutationIdentity(raw []byte) (string, string, error) {
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.UseNumber()
	var body map[string]any
	if err := dec.Decode(&body); err != nil {
		return "", "", err
	}
	if err := dec.Decode(&struct{}{}); err != io.EOF {
		return "", "", errors.New("trailing JSON")
	}
	key, ok := body["idempotency_key"].(string)
	if !ok || key == "" || len(key) > 128 {
		return "", "", errors.New("idempotency key missing")
	}
	canonical, err := json.Marshal(body)
	if err != nil {
		return "", "", err
	}
	sum := sha256.Sum256(canonical)
	return key, "sha256:" + hex.EncodeToString(sum[:]), nil
}

func beginMutation(ctx context.Context, pool *db.Pool, actor security.Actor, key, requestDigest string) (idempotencyDisposition, storedMutationResponse, error) {
	var disposition idempotencyDisposition
	var replay storedMutationResponse
	err := pool.WithTx(ctx, []db.TxOption{db.Serializable()}, func(tx *db.Tx) error {
		tag, err := tx.Exec(ctx, `INSERT INTO api_mutation_idempotency(
		 actor_issuer,actor_subject,idempotency_key,request_digest,status)
		 VALUES($1,$2,$3,$4,'in_progress') ON CONFLICT DO NOTHING`, actor.Issuer, actor.Subject, key, requestDigest)
		if err != nil {
			return err
		}
		if tag.RowsAffected() == 1 {
			disposition = idempotencyExecute
			return nil
		}
		var storedDigest, status string
		var responseStatus *int
		var responseBody []byte
		if err := tx.QueryRow(ctx, `SELECT request_digest,status,response_status,response_body
		 FROM api_mutation_idempotency WHERE actor_issuer=$1 AND actor_subject=$2 AND idempotency_key=$3 FOR UPDATE`,
			actor.Issuer, actor.Subject, key).Scan(&storedDigest, &status, &responseStatus, &responseBody); err != nil {
			return err
		}
		if storedDigest != requestDigest {
			disposition = idempotencyConflict
			return nil
		}
		if status == "completed" && responseStatus != nil && responseBody != nil {
			disposition = idempotencyReplay
			replay = storedMutationResponse{Status: *responseStatus, Body: append([]byte(nil), responseBody...)}
			return nil
		}
		disposition = idempotencyInProgress
		return nil
	})
	return disposition, replay, err
}

func completeMutation(ctx context.Context, pool *db.Pool, actor security.Actor, key, requestDigest string, status int, body []byte) error {
	if status < 100 || status > 599 || len(body) > maxMutationResponseBytes {
		return errors.New("mutation response outside durable bounds")
	}
	tag, err := pool.Pool.Exec(ctx, `UPDATE api_mutation_idempotency
	 SET status='completed',response_status=$1,response_body=$2,completed_at=now()
	 WHERE actor_issuer=$3 AND actor_subject=$4 AND idempotency_key=$5
	   AND request_digest=$6 AND status='in_progress'`, status, body, actor.Issuer, actor.Subject, key, requestDigest)
	if err != nil {
		return err
	}
	if tag.RowsAffected() != 1 {
		return errors.New("idempotency finalize CAS conflict")
	}
	return nil
}

// SweepMutationIdempotency removes at most limit completed response records
// older than the fixed retention. In-progress records are never auto-replayed or
// deleted because their business side effect may be unknown.
func SweepMutationIdempotency(ctx context.Context, pool *db.Pool, now time.Time, retention time.Duration, limit int) (int64, error) {
	if pool == nil || pool.Pool == nil || retention < 24*time.Hour || retention > 30*24*time.Hour || limit < 1 || limit > 1000 {
		return 0, errors.New("idempotency sweep arguments invalid")
	}
	tag, err := pool.Pool.Exec(ctx, `DELETE FROM api_mutation_idempotency WHERE ctid IN (
	 SELECT ctid FROM api_mutation_idempotency
		 WHERE status='completed' AND completed_at < $1 ORDER BY completed_at LIMIT $2)`, now.Add(-retention), limit)
	if err != nil {
		return 0, err
	}
	return tag.RowsAffected(), nil
}

type bufferedResponse struct {
	header   http.Header
	status   int
	body     bytes.Buffer
	overflow bool
}

func newBufferedResponse() *bufferedResponse    { return &bufferedResponse{header: make(http.Header)} }
func (b *bufferedResponse) Header() http.Header { return b.header }
func (b *bufferedResponse) WriteHeader(status int) {
	if b.status == 0 {
		b.status = status
	}
}
func (b *bufferedResponse) Write(p []byte) (int, error) {
	if b.status == 0 {
		b.status = http.StatusOK
	}
	if b.body.Len()+len(p) > maxMutationResponseBytes {
		b.overflow = true
		return 0, io.ErrShortWrite
	}
	return b.body.Write(p)
}
func (b *bufferedResponse) result() (int, []byte) {
	if b.overflow {
		return http.StatusInternalServerError, []byte(`{"error":"MUTATION_RESPONSE_TOO_LARGE"}` + "\n")
	}
	if b.status == 0 {
		b.status = http.StatusOK
	}
	return b.status, append([]byte(nil), b.body.Bytes()...)
}

func copyHeaders(dst, src http.Header) {
	for key, values := range src {
		for _, value := range values {
			dst.Add(key, value)
		}
	}
}
