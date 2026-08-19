package api

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net/http"
	"strconv"
	"sync"
	"time"

	"masi-nids/control-go/internal/security"
)

// SSEEventType / SSEResourceKind mirror the closed web/v1 unions.
type SSEEventType string

const (
	SSEInvalidate    SSEEventType = "invalidate"
	SSEHeartbeat     SSEEventType = "heartbeat"
	SSERefetchNeeded SSEEventType = "snapshot-refetch-required"
)

type SSEResourceKind string

const (
	ResEvent        SSEResourceKind = "event"
	ResIncident     SSEResourceKind = "incident"
	ResEvidence     SSEResourceKind = "evidence"
	ResProposal     SSEResourceKind = "proposal"
	ResDecision     SSEResourceKind = "decision"
	ResIntent       SSEResourceKind = "intent"
	ResFirewallRev  SSEResourceKind = "firewall-revision"
	ResTarget       SSEResourceKind = "target"
	ResFleetOp      SSEResourceKind = "fleet-operation"
	ResRuleEff      SSEResourceKind = "rule-effectiveness"
	ResModelRev     SSEResourceKind = "model-revision"
	ResModelBinding SSEResourceKind = "model-binding"
	ResPluginBind   SSEResourceKind = "plugin-binding"
	ResPluginRun    SSEResourceKind = "plugin-statistics-run"
	ResPluginCur    SSEResourceKind = "plugin-statistics-current"
	ResSystemHealth SSEResourceKind = "system-health"
)

// SSEEvent is the bounded invalidation envelope. ScopeID is an internal
// authorization dimension and is deliberately omitted from the browser wire;
// all other fields implement cursor/gap/generation recovery without carrying a
// credential or full business payload.
type SSEEvent struct {
	EventType        SSEEventType    `json:"event_type"`
	ResourceKind     SSEResourceKind `json:"resource_kind"`
	ResourceID       string          `json:"resource_id"`
	Cursor           string          `json:"cursor"`
	Sequence         uint64          `json:"sequence"`
	Generation       uint64          `json:"generation"`
	ProducedAtUnixMS int64           `json:"produced_at_unix_ms"`
	DataTimeUnixMS   int64           `json:"data_time_unix_ms"`
	PayloadDigest    string          `json:"payload_digest"`
	Bytes            int             `json:"bytes"`
	EmittedAtUnixMS  int64           `json:"emitted_at_unix_ms"`
	ScopeID          string          `json:"-"`
}

type subscription struct {
	scopes map[string]struct{}
}

// Hub is the bounded, scope-aware SSE fan-out. A subscriber receives only the
// resource scopes currently granted by the server-side RoleScopeMapping.
type Hub struct {
	mu         sync.Mutex
	subs       map[chan SSEEvent]subscription
	max        int
	now        func() time.Time
	sequence   uint64
	generation uint64
}

const defaultHubMaxSubscribers = 512

func NewHub() *Hub {
	return &Hub{
		subs: make(map[chan SSEEvent]subscription), max: defaultHubMaxSubscribers,
		now: time.Now, generation: 1,
	}
}

// Subscribe registers a bounded buffered subscriber for explicit scopes.
func (h *Hub) Subscribe(scopes []string) (chan SSEEvent, error) {
	h.mu.Lock()
	defer h.mu.Unlock()
	if len(h.subs) >= h.max {
		return nil, fmt.Errorf("api: sse subscriber bound %d reached", h.max)
	}
	if len(scopes) == 0 || len(scopes) > 256 {
		return nil, fmt.Errorf("api: sse subscriber requires 1..256 authorized scopes")
	}
	allowed := make(map[string]struct{}, len(scopes))
	for _, scope := range scopes {
		if scope == "" || len(scope) > 128 {
			return nil, fmt.Errorf("api: sse scope malformed")
		}
		allowed[scope] = struct{}{}
	}
	ch := make(chan SSEEvent, 64)
	h.subs[ch] = subscription{scopes: allowed}
	return ch, nil
}

// Unsubscribe removes a subscriber.
func (h *Hub) Unsubscribe(ch chan SSEEvent) {
	h.mu.Lock()
	defer h.mu.Unlock()
	if _, ok := h.subs[ch]; ok {
		delete(h.subs, ch)
		close(ch)
	}
}

// PublishScoped emits an invalidate event only to subscribers authorized for
// scopeID. Unscoped business invalidations are rejected instead of broadcast.
func (h *Hub) PublishScoped(kind SSEResourceKind, resourceID, scopeID string, dataTimeUnixMS int64, generation uint64) error {
	if !validResourceKind(kind) || kind == ResSystemHealth || resourceID == "" || len(resourceID) > 128 ||
		scopeID == "" || len(scopeID) > 128 {
		return fmt.Errorf("api: scoped SSE identity malformed")
	}
	h.mu.Lock()
	defer h.mu.Unlock()
	if generation == 0 {
		generation = h.generation
	}
	ev := h.newEventLocked(SSEInvalidate, kind, resourceID, scopeID, dataTimeUnixMS, generation)
	for ch, sub := range h.subs {
		if _, ok := sub.scopes[scopeID]; !ok {
			continue
		}
		select {
		case ch <- ev:
		default:
			// The channel is deliberately bounded. Replace one stale queued event
			// with an explicit refetch marker instead of silently hiding a gap.
			select {
			case <-ch:
			default:
			}
			refetch := h.newEventLocked(SSERefetchNeeded, kind, resourceID, scopeID, dataTimeUnixMS, generation)
			select {
			case ch <- refetch:
			default:
			}
		}
	}
	return nil
}

func (h *Hub) newEventLocked(eventType SSEEventType, kind SSEResourceKind, resourceID, scopeID string, dataTimeUnixMS int64, generation uint64) SSEEvent {
	h.sequence++
	now := h.now().UnixMilli()
	if dataTimeUnixMS <= 0 {
		dataTimeUnixMS = now
	}
	ev := SSEEvent{
		EventType: eventType, ResourceKind: kind, ResourceID: resourceID,
		Sequence: h.sequence, Generation: generation, ScopeID: scopeID,
		ProducedAtUnixMS: now, DataTimeUnixMS: dataTimeUnixMS, EmittedAtUnixMS: now,
	}
	ev.Cursor = "sse:" + strconv.FormatUint(generation, 10) + ":" + strconv.FormatUint(ev.Sequence, 10)
	return finalizeSSEEvent(ev)
}

// ServeEvents is the /events SSE endpoint. It derives the subscriber's scope
// set from the current server-side mapping and closes immediately on session
// expiry/logout/revocation.
func (h *Hub) ServeEvents(st *SessionStore, mapping *security.RoleScopeMapping, allowedOrigin string) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		session, ok := st.SessionFromRequest(r)
		if !ok {
			WriteError(w, http.StatusUnauthorized, "UNAUTHENTICATED")
			return
		}
		if err := VerifyOrigin(r, allowedOrigin); err != nil {
			WriteError(w, http.StatusForbidden, "CROSS_ORIGIN_REJECTED")
			return
		}
		if mapping == nil {
			WriteError(w, http.StatusForbidden, "SCOPE_MAPPING_UNAVAILABLE")
			return
		}
		actor := security.Actor{Issuer: session.Actor.Issuer, Subject: session.Actor.Subject}
		scopes, err := mapping.ScopeIDs(actor)
		if err != nil {
			WriteError(w, http.StatusForbidden, "SCOPE_DENIED")
			return
		}
		flusher, ok := w.(http.Flusher)
		if !ok {
			WriteError(w, http.StatusInternalServerError, "STREAMING_UNSUPPORTED")
			return
		}
		ch, err := h.Subscribe(scopes)
		if err != nil {
			WriteError(w, http.StatusServiceUnavailable, "SSE_SUBSCRIBER_BOUND")
			return
		}
		defer h.Unsubscribe(ch)
		revoked, cancelWatch, ok := st.WatchSession(session.ID)
		if !ok {
			WriteError(w, http.StatusUnauthorized, "SESSION_REVOKED")
			return
		}
		defer cancelWatch()

		w.Header().Set("Content-Type", "text/event-stream")
		w.Header().Set("Cache-Control", "no-store")
		w.Header().Set("X-Content-Type-Options", "nosniff")
		w.Header().Set("X-Accel-Buffering", "no")
		h.mu.Lock()
		initial := h.newEventLocked(SSERefetchNeeded, ResSystemHealth, "boot", "", h.now().UnixMilli(), h.generation)
		h.mu.Unlock()
		writeSSE(w, flusher, initial)

		heartbeat := time.NewTicker(SSEHeartbeatInterval)
		defer heartbeat.Stop()
		for {
			select {
			case <-r.Context().Done():
				return
			case <-revoked:
				return
			case ev, ok := <-ch:
				if !ok {
					return
				}
				writeSSE(w, flusher, ev)
			case <-heartbeat.C:
				h.mu.Lock()
				hb := h.newEventLocked(SSEHeartbeat, ResSystemHealth, "heartbeat", "", h.now().UnixMilli(), h.generation)
				h.mu.Unlock()
				writeSSE(w, flusher, hb)
			}
		}
	}
}

func writeSSE(w http.ResponseWriter, f http.Flusher, ev SSEEvent) {
	ev = finalizeSSEEvent(ev)
	payload, err := json.Marshal(ev)
	if err != nil || len(payload) > MaxSSEBytes {
		now := time.Now().UnixMilli()
		ev = SSEEvent{
			EventType: SSEHeartbeat, ResourceKind: ResSystemHealth, ResourceID: "heartbeat",
			Sequence: maxUint64(ev.Sequence, 1), Generation: maxUint64(ev.Generation, 1),
			ProducedAtUnixMS: now, DataTimeUnixMS: now, EmittedAtUnixMS: now,
		}
		ev.Cursor = "sse:" + strconv.FormatUint(ev.Generation, 10) + ":" + strconv.FormatUint(ev.Sequence, 10)
		ev = finalizeSSEEvent(ev)
		payload, _ = json.Marshal(ev)
	}
	_, _ = fmt.Fprintf(w, "event: %s\ndata: %s\n\n", ev.EventType, payload)
	f.Flush()
}

func finalizeSSEEvent(ev SSEEvent) SSEEvent {
	digestInput := fmt.Sprintf("%s|%s|%s|%s|%d|%d|%d|%d|%s", ev.EventType, ev.ResourceKind,
		ev.ResourceID, ev.Cursor, ev.Sequence, ev.Generation, ev.ProducedAtUnixMS, ev.DataTimeUnixMS, ev.ScopeID)
	h := sha256.Sum256([]byte(digestInput))
	ev.PayloadDigest = "sha256:" + hex.EncodeToString(h[:])
	if ev.EmittedAtUnixMS == 0 {
		ev.EmittedAtUnixMS = ev.ProducedAtUnixMS
	}
	for i := 0; i < 3; i++ {
		raw, err := json.Marshal(ev)
		if err != nil {
			return ev
		}
		if ev.Bytes == len(raw) {
			return ev
		}
		ev.Bytes = len(raw)
	}
	return ev
}

func validResourceKind(kind SSEResourceKind) bool {
	switch kind {
	case ResEvent, ResIncident, ResEvidence, ResProposal, ResDecision, ResIntent,
		ResFirewallRev, ResTarget, ResFleetOp, ResRuleEff, ResModelRev, ResModelBinding,
		ResPluginBind, ResPluginRun, ResPluginCur, ResSystemHealth:
		return true
	default:
		return false
	}
}

func maxUint64(v, minimum uint64) uint64 {
	if v < minimum {
		return minimum
	}
	return v
}
