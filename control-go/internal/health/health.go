// Package health implements startup, readiness, liveness, drain, and shutdown
// observation for Go Control Core. Health checks MUST NOT trigger mutation
// (ADR-0006 §9). Readiness reflects domain status; external P4/Central/Host/
// IdP local failures are reflected as domain status, not all converted to
// process not-ready. ready != business PASS.
package health

import (
	"context"
	"encoding/json"
	"net/http"
	"sync"
	"sync/atomic"
	"time"
)

// State is the observed process state. ready != PASS (a ready process may still
// hold domain HOLD/unknown facts).
type State string

const (
	StateStarting State = "starting"
	StateReady    State = "ready"
	StateDraining State = "draining"
	StateStopped  State = "stopped"
)

// DomainStatus is a per-domain readiness contribution. A domain may be
// unavailable without making the whole process not-ready (e.g. IdP JWKS
// unavailable -> OIDC domain down, but Event ingest may still be ready).
type DomainStatus struct {
	Domain string `json:"domain"`
	Status string `json:"status"` // ready|degraded|down|unknown
	Reason string `json:"reason,omitempty"`
}

// Checker observes process and domain state. It never mutates business facts.
type Checker struct {
	state              atomic.Value // State
	mu                 sync.RWMutex
	domains            map[string]DomainStatus
	startedAt          time.Time
	drainDeadline      time.Time
	lastProgressUnixMS atomic.Int64
}

// New constructs a Checker in the starting state.
func New() *Checker {
	c := &Checker{domains: map[string]DomainStatus{}, startedAt: time.Now()}
	c.state.Store(StateStarting)
	c.lastProgressUnixMS.Store(time.Now().UnixMilli())
	return c
}

// SetState transitions the process state. Drain sets a deadline after which
// in-flight work is abandoned (bounded; no blind external retry at shutdown).
func (c *Checker) SetState(s State, drainTimeout time.Duration) {
	c.state.Store(s)
	if s == StateDraining {
		c.mu.Lock()
		c.drainDeadline = time.Now().Add(drainTimeout)
		c.mu.Unlock()
	}
}

func (c *Checker) State() State { return c.state.Load().(State) }

func (c *Checker) AcceptingMutations() bool { return c.State() == StateReady }

func (c *Checker) MarkProgress() { c.lastProgressUnixMS.Store(time.Now().UnixMilli()) }

// SetDomain records a domain readiness contribution (no mutation).
func (c *Checker) SetDomain(d DomainStatus) {
	c.mu.Lock()
	c.domains[d.Domain] = d
	c.mu.Unlock()
}

// HandleLive is the liveness probe: the scheduler/HTTP/gRPC/DB pools making
// progress. Liveness does NOT fail on transient external module failures.
func (c *Checker) HandleLive(w http.ResponseWriter, _ *http.Request) {
	w.Header().Set("Content-Type", "application/json")
	state := c.state.Load().(State)
	progressAge := time.Now().UnixMilli() - c.lastProgressUnixMS.Load()
	live := state != StateStopped && (state != StateReady || progressAge <= 30_000)
	if !live {
		w.WriteHeader(http.StatusServiceUnavailable)
	}
	json.NewEncoder(w).Encode(map[string]any{"state": state, "ok": live, "progress_age_ms": progressAge})
}

// HandleReady is the readiness probe: required DB reachable + API served +
// schema/profile compatible. External domain degradations are reported but do
// not necessarily block readiness.
func (c *Checker) HandleReady(w http.ResponseWriter, _ *http.Request) {
	w.Header().Set("Content-Type", "application/json")
	state := c.state.Load().(State)
	c.mu.RLock()
	domains := make([]DomainStatus, 0, len(c.domains))
	for _, d := range c.domains {
		domains = append(domains, d)
	}
	c.mu.RUnlock()
	ready := state == StateReady
	code := http.StatusOK
	if !ready {
		code = http.StatusServiceUnavailable
	}
	w.WriteHeader(code)
	json.NewEncoder(w).Encode(map[string]any{
		"state":   state,
		"ready":   ready,
		"domains": domains,
		"note":    "ready != business PASS",
	})
}

// HandleHealth is the startup/health summary endpoint.
func (c *Checker) HandleHealth(w http.ResponseWriter, _ *http.Request) {
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(map[string]any{
		"state":      c.state.Load().(State),
		"started_at": c.startedAt.UTC().Format(time.RFC3339),
	})
}

// Drain blocks until in-flight work completes or the drain deadline expires.
// It stops new mutation/claim, releases leases, and never blindly retries
// external actions at shutdown (go-control-core-design §12).
func (c *Checker) Drain(ctx context.Context, wait func(context.Context) error) error {
	c.SetState(StateDraining, 0)
	dctx, cancel := context.WithDeadline(ctx, c.drainDeadlineTime())
	defer cancel()
	if err := wait(dctx); err != nil {
		c.SetState(StateStopped, 0)
		return err
	}
	c.SetState(StateStopped, 0)
	return nil
}

func (c *Checker) drainDeadlineTime() time.Time {
	c.mu.RLock()
	defer c.mu.RUnlock()
	if c.drainDeadline.IsZero() {
		return time.Now().Add(30 * time.Second)
	}
	return c.drainDeadline
}
