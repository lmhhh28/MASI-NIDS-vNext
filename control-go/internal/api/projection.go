package api

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strconv"
	"strings"
)

// Projection is the web/v1 response envelope (contracts/web/v1): cursor,
// bounded page, generation, scopes, and never-credential-carrying fields.
type Projection struct {
	SchemaVersion   string    `json:"schema_version"`
	ProjectionType  string    `json:"projection_type"`
	ResourceKind    string    `json:"resource_kind"`
	ResourceID      string    `json:"resource_id,omitempty"`
	Cursor          string    `json:"cursor"`
	PageSize        int       `json:"page_size"`
	TotalCount      int64     `json:"total_count"`
	Items           []any     `json:"items"`
	Generation      int64     `json:"generation"`
	SessionScope    string    `json:"session_scope"`
	AuthorizedScope string    `json:"authorized_scope"`
	SSEEvent        *SSEEvent `json:"sse_event"`
	ActorRef        string    `json:"actor_ref"`
	ReasonCode      string    `json:"reason_code"`
	TraceID         string    `json:"trace_id"`
}

// ProjectionType values (web/v1).
const (
	ProjectionCurrent     = "current"
	ProjectionDesired     = "desired"
	ProjectionObserved    = "observed"
	ProjectionStale       = "stale"
	ProjectionHold        = "hold"
	ProjectionUnknown     = "unknown"
	ProjectionReconciling = "reconciling"
)

// Page-size bounds (web/v1: <= 200; default 50).
const (
	DefaultPageSize = 50
	MaxPageSize     = 200
)

// Cursor is the opaque server cursor payload (base64(JSON)). It carries NO
// credentials and no offsets the client can forge ordering from.
type Cursor struct {
	LastID     int64  `json:"last_id,omitempty"`
	LastKey    string `json:"last_key,omitempty"`
	Forward    bool   `json:"forward"`
	Generation int64  `json:"generation,omitempty"`
	Kind       string `json:"kind,omitempty"`
	ScopeHash  string `json:"scope_hash,omitempty"`
}

// CursorCodec authenticates cursor payloads. A cursor is opaque navigation
// state, never client authority; binding kind+scope prevents replay across a
// different projection or authorization mapping.
type CursorCodec struct{ key []byte }

func NewCursorCodec(key []byte) (*CursorCodec, error) {
	if len(key) < 32 || len(key) > 128 {
		return nil, errors.New("api: cursor HMAC key must be 32..128 bytes")
	}
	return &CursorCodec{key: append([]byte(nil), key...)}, nil
}

// Encode makes the authenticated opaque wire form.
func (c *CursorCodec) Encode(cursor Cursor) (string, error) {
	if c == nil || len(c.key) < 32 {
		return "", errors.New("api: cursor codec unavailable")
	}
	raw, err := json.Marshal(cursor)
	if err != nil || len(raw) > 320 {
		return "", errors.New("api: cursor payload too large")
	}
	mac := hmac.New(sha256.New, c.key)
	_, _ = mac.Write(raw)
	return base64.RawURLEncoding.EncodeToString(raw) + "." +
		base64.RawURLEncoding.EncodeToString(mac.Sum(nil)), nil
}

// Decode verifies and parses the opaque form; "" is the first page.
func (c *CursorCodec) Decode(s string) (Cursor, error) {
	if s == "" {
		return Cursor{Forward: true}, nil
	}
	if c == nil || len(c.key) < 32 || len(s) > 512 {
		return Cursor{}, errors.New("api: cursor unavailable or too large")
	}
	parts := strings.Split(s, ".")
	if len(parts) != 2 {
		return Cursor{}, errors.New("api: cursor malformed")
	}
	raw, err := base64.RawURLEncoding.DecodeString(parts[0])
	if err != nil {
		return Cursor{}, fmt.Errorf("api: cursor malformed")
	}
	signature, err := base64.RawURLEncoding.DecodeString(parts[1])
	if err != nil {
		return Cursor{}, fmt.Errorf("api: cursor signature malformed")
	}
	mac := hmac.New(sha256.New, c.key)
	_, _ = mac.Write(raw)
	if !hmac.Equal(signature, mac.Sum(nil)) {
		return Cursor{}, errors.New("api: cursor signature mismatch")
	}
	var cursor Cursor
	dec := json.NewDecoder(strings.NewReader(string(raw)))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&cursor); err != nil {
		return Cursor{}, fmt.Errorf("api: cursor payload malformed")
	}
	if err := dec.Decode(&struct{}{}); err != io.EOF {
		return Cursor{}, errors.New("api: cursor trailing content")
	}
	if cursor.LastID < 0 || cursor.Generation < 0 || len(cursor.LastKey) > 128 || len(cursor.Kind) > 64 ||
		(cursor.ScopeHash != "" && !validCursorDigest(cursor.ScopeHash)) {
		return Cursor{}, errors.New("api: cursor fields outside bounds")
	}
	return cursor, nil
}

func validCursorDigest(value string) bool {
	if len(value) != 71 || !strings.HasPrefix(value, "sha256:") {
		return false
	}
	for _, r := range value[len("sha256:"):] {
		if (r < '0' || r > '9') && (r < 'a' || r > 'f') {
			return false
		}
	}
	return true
}

// ParsePageSize validates the bounded page size.
func ParsePageSize(r *http.Request) (int, error) {
	raw := r.URL.Query().Get("page_size")
	if raw == "" {
		return DefaultPageSize, nil
	}
	n, err := strconv.Atoi(raw)
	if err != nil || n < 1 || n > MaxPageSize {
		return 0, fmt.Errorf("api: page_size must be 1..%d", MaxPageSize)
	}
	return n, nil
}

// WriteProjection emits the envelope JSON with no-store caching.
func WriteProjection(w http.ResponseWriter, p Projection) {
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Cache-Control", "no-store")
	if err := json.NewEncoder(w).Encode(p); err != nil {
		http.Error(w, `{"error":"PROJECTION_ENCODE_ERROR"}`, http.StatusInternalServerError)
	}
}

// WriteError emits the stable error namespace with a bounded message.
func WriteError(w http.ResponseWriter, status int, reason string) {
	reason = strings.ToUpper(strings.ReplaceAll(reason, " ", "_"))
	if len(reason) > 64 {
		reason = reason[:64]
	}
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Cache-Control", "no-store")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(map[string]string{"error": reason})
}

// NewProjection fills the common envelope fields.
func NewProjection(kind, projectionType string, pageSize int, total int64, generation int64, actorRef, scope string) Projection {
	return Projection{
		SchemaVersion: "masi-web-projection/v1", ProjectionType: projectionType,
		ResourceKind: kind, PageSize: pageSize, TotalCount: total,
		Items: []any{}, Generation: generation, SessionScope: scope,
		AuthorizedScope: scope, ActorRef: actorRef, ReasonCode: "OK",
	}
}
