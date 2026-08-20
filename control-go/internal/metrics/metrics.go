// Package metrics exposes a bounded, low-cardinality Prometheus endpoint for
// Control Core. Canonical business facts stay in PostgreSQL; these values are
// operational observations only and never feed authorization or mutation.
package metrics

import (
	"bufio"
	"context"
	"crypto/sha256"
	"crypto/x509"
	"encoding/hex"
	"encoding/json"
	"encoding/pem"
	"errors"
	"fmt"
	"net/http"
	"os"
	"runtime"
	runtimemetrics "runtime/metrics"
	"sort"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"masi-nids/control-go/internal/db"
)

type key struct {
	group, method, class string
}

var httpDurationBounds = [...]float64{0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5}

const (
	maxHTTPSeries   = 256
	maxActiveSeries = 5000
)

type requestMetric struct {
	count         uint64
	requestBytes  uint64
	responseBytes uint64
	durationSum   float64
	buckets       [len(httpDurationBounds)]uint64
}

// Registry owns only fixed-enum counters. It never labels by target, actor,
// operation, digest, URL/query, plugin, model, or other unbounded identity.
type Registry struct {
	pool                *db.Pool
	started             time.Time
	inFlight            atomic.Int64
	mu                  sync.Mutex
	requests            map[key]*requestMetric
	overflow            uint64
	configPath          string
	configDigest        string
	httpCertificateFile string
	grpcCertificateFile string
}

func New(pool *db.Pool) *Registry {
	return &Registry{pool: pool, started: time.Now(), requests: make(map[key]*requestMetric, 128)}
}

// ConfigureRuntimeFiles binds drift/expiry metrics to the exact files accepted
// at startup. File contents and digests are never exposed as labels.
func (m *Registry) ConfigureRuntimeFiles(configPath, httpCertificateFile, grpcCertificateFile string) error {
	if configPath == "" {
		return errors.New("metrics: runtime config path required")
	}
	raw, err := os.ReadFile(configPath)
	if err != nil {
		return fmt.Errorf("metrics: read runtime config: %w", err)
	}
	if len(raw) == 0 || len(raw) > 4*1024*1024 {
		return errors.New("metrics: runtime config size outside 1..4194304 bytes")
	}
	sum := sha256.Sum256(raw)
	m.mu.Lock()
	m.configPath = configPath
	m.configDigest = "sha256:" + hex.EncodeToString(sum[:])
	m.httpCertificateFile = httpCertificateFile
	m.grpcCertificateFile = grpcCertificateFile
	m.mu.Unlock()
	return nil
}

func (m *Registry) Middleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		started := time.Now()
		m.inFlight.Add(1)
		defer m.inFlight.Add(-1)
		recorder := &statusRecorder{ResponseWriter: w, status: http.StatusOK}
		next.ServeHTTP(recorder, r)
		k := key{group: routeGroup(r.URL.Path), method: boundedMethod(r.Method), class: statusClass(recorder.status)}
		m.mu.Lock()
		metric := m.requests[k]
		if metric == nil {
			if len(m.requests) >= maxHTTPSeries {
				m.overflow++
				m.mu.Unlock()
				return
			}
			metric = &requestMetric{}
			m.requests[k] = metric
		}
		metric.count++
		if r.ContentLength > 0 {
			metric.requestBytes += uint64(r.ContentLength)
		}
		metric.responseBytes += recorder.bytes
		duration := time.Since(started).Seconds()
		metric.durationSum += duration
		for index, bound := range httpDurationBounds {
			if duration <= bound {
				metric.buckets[index]++
			}
		}
		m.mu.Unlock()
	})
}

type statusRecorder struct {
	http.ResponseWriter
	status      int
	bytes       uint64
	wroteHeader bool
}

func (w *statusRecorder) Unwrap() http.ResponseWriter { return w.ResponseWriter }

func (w *statusRecorder) Flush() {
	if !w.wroteHeader {
		w.WriteHeader(http.StatusOK)
	}
	if flusher, ok := w.ResponseWriter.(http.Flusher); ok {
		flusher.Flush()
	}
}

func (w *statusRecorder) WriteHeader(status int) {
	if w.wroteHeader {
		return
	}
	w.wroteHeader = true
	w.status = status
	w.ResponseWriter.WriteHeader(status)
}

func (w *statusRecorder) Write(body []byte) (int, error) {
	if !w.wroteHeader {
		w.WriteHeader(http.StatusOK)
	}
	n, err := w.ResponseWriter.Write(body)
	w.bytes += uint64(n)
	return n, err
}

func routeGroup(path string) string {
	switch {
	case path == "/healthz" || path == "/readyz" || path == "/livez":
		return "probe"
	case path == "/metrics":
		return "metrics"
	case path == "/events":
		return "events"
	case strings.HasPrefix(path, "/oidc/"):
		return "oidc"
	case strings.HasPrefix(path, "/mcp"):
		return "mcp"
	case strings.HasPrefix(path, "/api/events"):
		return "api_events"
	case strings.HasPrefix(path, "/api/effects"):
		return "api_effects"
	case strings.HasPrefix(path, "/api/firewall"):
		return "api_firewall"
	case strings.HasPrefix(path, "/api/targets"):
		return "api_targets"
	case strings.HasPrefix(path, "/api/fleet"):
		return "api_fleet"
	case strings.HasPrefix(path, "/api/models"):
		return "api_models"
	case strings.HasPrefix(path, "/api/plugins"):
		return "api_plugins"
	case strings.HasPrefix(path, "/api/rule-effectiveness"):
		return "api_rules"
	case strings.HasPrefix(path, "/api/"):
		return "api_other"
	default:
		return "other"
	}
}

func boundedMethod(method string) string {
	switch method {
	case http.MethodGet, http.MethodPost:
		return method
	default:
		return "OTHER"
	}
}

func statusClass(status int) string {
	if status < 100 || status > 599 {
		return "invalid"
	}
	return strconv.Itoa(status/100) + "xx"
}

func (m *Registry) Handler(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
	w.Header().Set("Cache-Control", "no-store")
	w.Header().Set("X-Content-Type-Options", "nosniff")
	bw := bufio.NewWriterSize(w, 32*1024)
	defer bw.Flush()

	var mem runtime.MemStats
	runtime.ReadMemStats(&mem)
	fmt.Fprintf(bw, "# HELP masi_control_process_uptime_seconds Process monotonic uptime.\n")
	fmt.Fprintf(bw, "# TYPE masi_control_process_uptime_seconds gauge\nmasi_control_process_uptime_seconds %.3f\n", time.Since(m.started).Seconds())
	fmt.Fprintf(bw, "# TYPE masi_control_go_goroutines gauge\nmasi_control_go_goroutines %d\n", runtime.NumGoroutine())
	fmt.Fprintf(bw, "# TYPE masi_control_go_heap_bytes gauge\nmasi_control_go_heap_bytes %d\n", mem.HeapAlloc)
	if cpu, ok := processCPUSeconds(); ok {
		fmt.Fprintf(bw, "# TYPE masi_control_process_cpu_seconds_total counter\nmasi_control_process_cpu_seconds_total %.6f\n", cpu)
	}
	if rss, ok := residentBytes(); ok {
		fmt.Fprintf(bw, "# TYPE masi_control_process_resident_memory_bytes gauge\nmasi_control_process_resident_memory_bytes %d\n", rss)
	}
	if fds, ok := fileDescriptorCount(); ok {
		fmt.Fprintf(bw, "# TYPE masi_control_process_open_fds gauge\nmasi_control_process_open_fds %d\n", fds)
	}
	if threads, ok := threadCount(); ok {
		fmt.Fprintf(bw, "# TYPE masi_control_process_threads gauge\nmasi_control_process_threads %d\n", threads)
	}
	fmt.Fprintf(bw, "# TYPE masi_control_http_in_flight gauge\nmasi_control_http_in_flight %d\n", m.inFlight.Load())

	m.mu.Lock()
	type requestSnapshotEntry struct {
		key
		requestMetric
	}
	requestSnapshot := make([]requestSnapshotEntry, 0, len(m.requests))
	for k, value := range m.requests {
		requestSnapshot = append(requestSnapshot, requestSnapshotEntry{key: k, requestMetric: *value})
	}
	overflow := m.overflow
	m.mu.Unlock()
	sort.Slice(requestSnapshot, func(i, j int) bool {
		if requestSnapshot[i].group != requestSnapshot[j].group {
			return requestSnapshot[i].group < requestSnapshot[j].group
		}
		if requestSnapshot[i].method != requestSnapshot[j].method {
			return requestSnapshot[i].method < requestSnapshot[j].method
		}
		return requestSnapshot[i].class < requestSnapshot[j].class
	})
	fmt.Fprintln(bw, "# TYPE masi_control_http_requests_total counter")
	fmt.Fprintln(bw, "# TYPE masi_control_http_request_bytes_total counter")
	fmt.Fprintln(bw, "# TYPE masi_control_http_response_bytes_total counter")
	fmt.Fprintln(bw, "# TYPE masi_control_http_request_duration_seconds histogram")
	for _, item := range requestSnapshot {
		fmt.Fprintf(bw, "masi_control_http_requests_total{route_group=%q,method=%q,status_class=%q} %d\n",
			item.group, item.method, item.class, item.count)
		fmt.Fprintf(bw, "masi_control_http_request_bytes_total{route_group=%q,method=%q,status_class=%q} %d\n",
			item.group, item.method, item.class, item.requestBytes)
		fmt.Fprintf(bw, "masi_control_http_response_bytes_total{route_group=%q,method=%q,status_class=%q} %d\n",
			item.group, item.method, item.class, item.responseBytes)
		for index, bound := range httpDurationBounds {
			fmt.Fprintf(bw, "masi_control_http_request_duration_seconds_bucket{route_group=%q,method=%q,status_class=%q,le=%q} %d\n",
				item.group, item.method, item.class, strconv.FormatFloat(bound, 'g', -1, 64), item.buckets[index])
		}
		fmt.Fprintf(bw, "masi_control_http_request_duration_seconds_bucket{route_group=%q,method=%q,status_class=%q,le=\"+Inf\"} %d\n",
			item.group, item.method, item.class, item.count)
		fmt.Fprintf(bw, "masi_control_http_request_duration_seconds_sum{route_group=%q,method=%q,status_class=%q} %.9f\n",
			item.group, item.method, item.class, item.durationSum)
		fmt.Fprintf(bw, "masi_control_http_request_duration_seconds_count{route_group=%q,method=%q,status_class=%q} %d\n",
			item.group, item.method, item.class, item.count)
	}
	fmt.Fprintf(bw, "# TYPE masi_control_metrics_series_overflow_total counter\nmasi_control_metrics_series_overflow_total %d\n", overflow)
	fmt.Fprintf(bw, "# TYPE masi_control_metrics_active_series_limit gauge\nmasi_control_metrics_active_series_limit %d\n", maxActiveSeries)
	m.writeRuntimeIntegrityMetrics(bw)

	if m.pool == nil || m.pool.Pool == nil {
		return
	}
	stat := m.pool.Pool.Stat()
	fmt.Fprintln(bw, "# TYPE masi_control_postgresql_connections gauge")
	fmt.Fprintf(bw, "masi_control_postgresql_connections{state=\"acquired\"} %d\n", stat.AcquiredConns())
	fmt.Fprintf(bw, "masi_control_postgresql_connections{state=\"idle\"} %d\n", stat.IdleConns())
	fmt.Fprintf(bw, "masi_control_postgresql_connections{state=\"total\"} %d\n", stat.TotalConns())
	fmt.Fprintf(bw, "# TYPE masi_control_postgresql_acquire_total counter\nmasi_control_postgresql_acquire_total %d\n", stat.AcquireCount())
	fmt.Fprintf(bw, "# TYPE masi_control_postgresql_acquire_duration_seconds counter\nmasi_control_postgresql_acquire_duration_seconds %.6f\n", stat.AcquireDuration().Seconds())

	startedScrape := time.Now()
	if err := m.writeDatabaseMetrics(bw, r); err != nil {
		fmt.Fprintln(bw, "# TYPE masi_control_metrics_postgresql_scrape_error gauge\nmasi_control_metrics_postgresql_scrape_error 1")
		fmt.Fprintf(bw, "# TYPE masi_control_metrics_postgresql_scrape_duration_seconds gauge\nmasi_control_metrics_postgresql_scrape_duration_seconds %.6f\n", time.Since(startedScrape).Seconds())
		return
	}
	fmt.Fprintln(bw, "# TYPE masi_control_metrics_postgresql_scrape_error gauge\nmasi_control_metrics_postgresql_scrape_error 0")
	fmt.Fprintf(bw, "# TYPE masi_control_metrics_postgresql_scrape_duration_seconds gauge\nmasi_control_metrics_postgresql_scrape_duration_seconds %.6f\n", time.Since(startedScrape).Seconds())
}

func (m *Registry) writeRuntimeIntegrityMetrics(bw *bufio.Writer) {
	m.mu.Lock()
	configPath, expected := m.configPath, m.configDigest
	httpCertificateFile, grpcCertificateFile := m.httpCertificateFile, m.grpcCertificateFile
	m.mu.Unlock()
	drift := 1
	if raw, err := os.ReadFile(configPath); err == nil && len(raw) > 0 && len(raw) <= 4*1024*1024 {
		sum := sha256.Sum256(raw)
		if "sha256:"+hex.EncodeToString(sum[:]) == expected {
			drift = 0
		}
	}
	fmt.Fprintf(bw, "# TYPE masi_control_config_drift gauge\nmasi_control_config_drift %d\n", drift)
	fmt.Fprintln(bw, "# TYPE masi_control_certificate_expiry_seconds gauge")
	fmt.Fprintln(bw, "# TYPE masi_control_certificate_read_error gauge")
	for _, item := range []struct{ endpoint, path string }{{"http", httpCertificateFile}, {"grpc", grpcCertificateFile}} {
		expiry, err := certificateExpiry(item.path)
		if err != nil {
			fmt.Fprintf(bw, "masi_control_certificate_expiry_seconds{endpoint=%q} 0\n", item.endpoint)
			fmt.Fprintf(bw, "masi_control_certificate_read_error{endpoint=%q} 1\n", item.endpoint)
			continue
		}
		remaining := time.Until(expiry).Seconds()
		fmt.Fprintf(bw, "masi_control_certificate_expiry_seconds{endpoint=%q} %.3f\n", item.endpoint, remaining)
		fmt.Fprintf(bw, "masi_control_certificate_read_error{endpoint=%q} 0\n", item.endpoint)
	}
}

func certificateExpiry(path string) (time.Time, error) {
	if path == "" {
		return time.Time{}, errors.New("certificate path not configured")
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		return time.Time{}, err
	}
	if len(raw) == 0 || len(raw) > 1024*1024 {
		return time.Time{}, errors.New("certificate file outside bound")
	}
	block, _ := pem.Decode(raw)
	if block == nil || block.Type != "CERTIFICATE" {
		return time.Time{}, errors.New("certificate PEM missing")
	}
	certificate, err := x509.ParseCertificate(block.Bytes)
	if err != nil {
		return time.Time{}, err
	}
	return certificate.NotAfter, nil
}

type gaugeSpec struct {
	key, name, labelName, labelValue string
}

func (m *Registry) writeDatabaseMetrics(bw *bufio.Writer, r *http.Request) error {
	ctx, cancel := context.WithTimeout(r.Context(), time.Second)
	defer cancel()
	nowMS := time.Now().UnixMilli()
	var raw []byte
	err := m.pool.Pool.QueryRow(ctx, `SELECT jsonb_build_object(
	 'effect_unclaimed',(SELECT count(*) FROM effect_intents WHERE NOT is_fleet_parent AND claim_state='unclaimed'),
	 'effect_claimed',(SELECT count(*) FROM effect_intents WHERE NOT is_fleet_parent AND claim_state='claimed'),
	 'effect_unknown',(SELECT count(*) FROM effect_intents WHERE NOT is_fleet_parent AND claim_state='unknown'),
	 'effect_hold',(SELECT count(*) FROM effect_intents WHERE NOT is_fleet_parent AND claim_state IN ('hold','blocked')),
	 'attempt_applied',(SELECT count(*) FROM effect_attempts WHERE status='applied'),
	 'attempt_hold',(SELECT count(*) FROM effect_attempts WHERE status='hold'),
	 'attempt_unknown',(SELECT count(*) FROM effect_attempts WHERE status='unknown'),
	 'ack_pending',(SELECT count(*) FROM effect_acknowledgements WHERE state='pending'),
	 'ack_exhausted',(SELECT count(*) FROM effect_acknowledgements WHERE state='exhausted'),
	 'proposal_pending',(SELECT count(*) FROM effect_proposals p WHERE p.expires_at_unix_ms>$1 AND p.superseded_by_proposal_id IS NULL
	   AND NOT EXISTS(SELECT 1 FROM effect_decisions d WHERE d.proposal_id=p.proposal_id)),
	 'proposal_expired',(SELECT count(*) FROM effect_proposals p WHERE p.expires_at_unix_ms<=$1
	   AND NOT EXISTS(SELECT 1 FROM effect_decisions d WHERE d.proposal_id=p.proposal_id)),
	 'decision_approve',(SELECT count(*) FROM effect_decisions WHERE decision='approve'),
	 'decision_reject',(SELECT count(*) FROM effect_decisions WHERE decision='reject'),
	 'target_candidate',(SELECT count(*) FROM targets WHERE status='candidate'),
	 'target_active',(SELECT count(*) FROM targets WHERE status='active'),
	 'target_draining',(SELECT count(*) FROM targets WHERE status='draining'),
	 'target_disabled',(SELECT count(*) FROM targets WHERE status='disabled'),
	 'target_quarantined',(SELECT count(*) FROM targets WHERE status='quarantined'),
	 'target_disconnected',(SELECT count(*) FROM target_capability_observations WHERE NOT p4_connected),
	 'target_not_primary',(SELECT count(*) FROM target_capability_observations WHERE NOT primary_actor),
	 'target_pipeline_drift',(SELECT count(*) FROM target_capability_observations WHERE NOT pipeline_exact),
	 'firewall_bound',(SELECT count(*) FROM firewall_bindings WHERE current_revision_id IS NOT NULL),
	 'firewall_reconciling',(SELECT count(*) FROM firewall_bindings WHERE selector_state='reconciling'),
	 'overlay_active',(SELECT count(*) FROM firewall_overlays WHERE NOT deleted AND expires_at_unix_ms>$1),
	 'overlay_expired',(SELECT count(*) FROM firewall_overlays WHERE NOT deleted AND expires_at_unix_ms<=$1),
	 'capture_active',(SELECT count(*) FROM bounded_capture_requests WHERE state IN ('authorized','claimed','executing','unknown')),
	 'fleet_in_progress',(SELECT count(*) FROM fleet_operations WHERE aggregate_status NOT IN ('applied','failed','blocked')),
	 'fleet_child_blocked',(SELECT count(*) FROM fleet_child_intents WHERE status='blocked'),
	 'model_in_progress',(SELECT count(*) FROM model_rollout_operations WHERE status NOT IN ('applied','failed','aborted')),
	 'plugin_active',(SELECT count(*) FROM plugin_bindings WHERE activation_state='active'),
	 'plugin_quarantined',(SELECT count(*) FROM plugin_bindings WHERE activation_state IN ('stale','unqualified')),
	 'plugin_qualification_hold',(SELECT count(*) FROM plugin_qualifications WHERE qualification_status='hold'),
	 'stat_pending',(SELECT count(*) FROM plugin_statistic_runs WHERE status='pending'),
	 'stat_running',(SELECT count(*) FROM plugin_statistic_runs WHERE status='running'),
	 'stat_failed',(SELECT count(*) FROM plugin_statistic_runs WHERE status IN ('failed','timeout')),
	 'stat_quality_degraded',(SELECT count(*) FROM plugin_statistic_artifacts WHERE quality IN ('partial','gap','stale','not_measurable','invalid')),
	 'analysis_working',(SELECT count(*) FROM analysis_task_requests WHERE status IN ('submitted','working')),
	 'analysis_limited',(SELECT count(*) FROM analysis_task_requests WHERE status IN ('limited','insufficient_evidence')),
	 'analysis_failed',(SELECT count(*) FROM analysis_task_requests WHERE status IN ('failed','fenced','unknown')),
	 'mcp_denied',(SELECT count(*) FROM mcp_access_audit WHERE outcome='denied'),
	 'mcp_failed',(SELECT count(*) FROM mcp_access_audit WHERE outcome='failed'),
	 'rule_quality_degraded',(SELECT count(*) FROM rule_observations WHERE quality_status IN ('gap','stale','not-measurable','invalid','reset')),
	 'event_commits_last_minute',(SELECT count(*) FROM event_identities WHERE created_at>=now()-interval '1 minute'),
	 'proposal_oldest_seconds',COALESCE((SELECT max(($1-created_at_unix_ms)/1000.0) FROM effect_proposals p
	   WHERE p.expires_at_unix_ms>$1 AND NOT EXISTS(SELECT 1 FROM effect_decisions d WHERE d.proposal_id=p.proposal_id)),0),
	 'effect_oldest_seconds',COALESCE((SELECT max(extract(epoch FROM (now()-created_at))) FROM effect_intents
	   WHERE NOT is_fleet_parent AND claim_state='unclaimed'),0),
	 'stat_oldest_seconds',COALESCE((SELECT max(extract(epoch FROM (now()-created_at))) FROM plugin_statistic_runs
	   WHERE status IN ('pending','running')),0)
	 )`, nowMS).Scan(&raw)
	if err != nil {
		return err
	}
	values := map[string]float64{}
	if err := json.Unmarshal(raw, &values); err != nil {
		return err
	}
	specs := []gaugeSpec{
		{"effect_unclaimed", "masi_control_effect_intents", "state", "unclaimed"},
		{"effect_claimed", "masi_control_effect_intents", "state", "claimed"},
		{"effect_unknown", "masi_control_effect_intents", "state", "unknown"},
		{"effect_hold", "masi_control_effect_intents", "state", "hold_or_blocked"},
		{"attempt_applied", "masi_control_effect_attempts", "result", "applied"},
		{"attempt_hold", "masi_control_effect_attempts", "result", "hold"},
		{"attempt_unknown", "masi_control_effect_attempts", "result", "unknown"},
		{"ack_pending", "masi_control_effect_acknowledgements", "state", "pending"},
		{"ack_exhausted", "masi_control_effect_acknowledgements", "state", "exhausted"},
		{"proposal_pending", "masi_control_effect_proposals", "state", "pending"},
		{"proposal_expired", "masi_control_effect_proposals", "state", "expired"},
		{"decision_approve", "masi_control_effect_decisions", "result", "approve"},
		{"decision_reject", "masi_control_effect_decisions", "result", "reject"},
		{"target_candidate", "masi_control_targets", "state", "candidate"},
		{"target_active", "masi_control_targets", "state", "active"},
		{"target_draining", "masi_control_targets", "state", "draining"},
		{"target_disabled", "masi_control_targets", "state", "disabled"},
		{"target_quarantined", "masi_control_targets", "state", "quarantined"},
		{"target_disconnected", "masi_control_target_readiness", "reason", "p4_disconnected"},
		{"target_not_primary", "masi_control_target_readiness", "reason", "not_primary"},
		{"target_pipeline_drift", "masi_control_target_readiness", "reason", "pipeline_drift"},
		{"firewall_bound", "masi_control_firewall_bindings", "state", "active"},
		{"firewall_reconciling", "masi_control_firewall_bindings", "state", "reconciling"},
		{"overlay_active", "masi_control_firewall_overlays", "state", "active"},
		{"overlay_expired", "masi_control_firewall_overlays", "state", "expiry_backlog"},
		{"capture_active", "masi_control_bounded_captures", "state", "in_progress"},
		{"fleet_in_progress", "masi_control_fleet_operations", "state", "in_progress"},
		{"fleet_child_blocked", "masi_control_fleet_children", "state", "blocked"},
		{"model_in_progress", "masi_control_model_operations", "state", "in_progress"},
		{"plugin_active", "masi_control_plugin_bindings", "state", "active"},
		{"plugin_quarantined", "masi_control_plugin_bindings", "state", "unavailable"},
		{"plugin_qualification_hold", "masi_control_plugin_qualifications", "result", "hold"},
		{"stat_pending", "masi_control_plugin_statistics_runs", "state", "pending"},
		{"stat_running", "masi_control_plugin_statistics_runs", "state", "running"},
		{"stat_failed", "masi_control_plugin_statistics_runs", "state", "failed_or_timeout"},
		{"stat_quality_degraded", "masi_control_plugin_statistics_artifacts", "quality", "degraded"},
		{"analysis_working", "masi_control_analysis_tasks", "outcome", "working"},
		{"analysis_limited", "masi_control_analysis_tasks", "outcome", "limited"},
		{"analysis_failed", "masi_control_analysis_tasks", "outcome", "failed"},
		{"mcp_denied", "masi_control_mcp_calls", "outcome", "denied"},
		{"mcp_failed", "masi_control_mcp_calls", "outcome", "failed"},
		{"rule_quality_degraded", "masi_control_rule_observations", "quality", "degraded"},
		{"event_commits_last_minute", "masi_control_event_commit_throughput_per_second", "", ""},
		{"proposal_oldest_seconds", "masi_control_effect_proposal_backlog_age_seconds", "", ""},
		{"effect_oldest_seconds", "masi_control_effect_intent_backlog_age_seconds", "", ""},
		{"stat_oldest_seconds", "masi_control_plugin_statistics_backlog_age_seconds", "", ""},
	}
	types := map[string]bool{}
	for _, spec := range specs {
		if !types[spec.name] {
			fmt.Fprintf(bw, "# TYPE %s gauge\n", spec.name)
			types[spec.name] = true
		}
		value := values[spec.key]
		if spec.key == "event_commits_last_minute" {
			value /= 60
		}
		if spec.labelName == "" {
			fmt.Fprintf(bw, "%s %.6f\n", spec.name, value)
		} else {
			fmt.Fprintf(bw, "%s{%s=%q} %.6f\n", spec.name, spec.labelName, spec.labelValue, value)
		}
	}
	if err := m.writeLatencyQuantiles(ctx, bw); err != nil {
		return err
	}
	return m.writePostgresRuntimeMetrics(ctx, bw)
}

func (m *Registry) writeLatencyQuantiles(ctx context.Context, bw *bufio.Writer) error {
	var effect, decision, statistics, analysis []float64
	err := m.pool.Pool.QueryRow(ctx, `SELECT
	 COALESCE((SELECT percentile_cont(ARRAY[0.5,0.95,0.99]) WITHIN GROUP
	   (ORDER BY (finished_at_unix_ms-started_at_unix_ms)/1000.0) FROM effect_attempts
	   WHERE finished_at_unix_ms IS NOT NULL),ARRAY[0::float8,0::float8,0::float8]),
	 COALESCE((SELECT percentile_cont(ARRAY[0.5,0.95,0.99]) WITHIN GROUP
	   (ORDER BY (d.created_at_unix_ms-p.created_at_unix_ms)/1000.0) FROM effect_decisions d
	   JOIN effect_proposals p ON p.proposal_id=d.proposal_id),ARRAY[0::float8,0::float8,0::float8]),
	 COALESCE((SELECT percentile_cont(ARRAY[0.5,0.95,0.99]) WITHIN GROUP
	   (ORDER BY (finished_at_unix_ms-started_at_unix_ms)/1000.0) FROM plugin_statistic_runs
	   WHERE finished_at_unix_ms IS NOT NULL),ARRAY[0::float8,0::float8,0::float8]),
	 COALESCE((SELECT percentile_cont(ARRAY[0.5,0.95,0.99]) WITHIN GROUP
	   (ORDER BY (updated_at_unix_ms-created_at_unix_ms)/1000.0) FROM analysis_task_requests
	   WHERE status NOT IN ('submitted','working')),ARRAY[0::float8,0::float8,0::float8])`).
		Scan(&effect, &decision, &statistics, &analysis)
	if err != nil {
		return err
	}
	fmt.Fprintln(bw, "# TYPE masi_control_operation_latency_seconds gauge")
	for operation, values := range map[string][]float64{"effect": effect, "decision": decision, "plugin_statistics": statistics, "analysis_a2a": analysis} {
		for index, quantile := range []string{"0.50", "0.95", "0.99"} {
			if index < len(values) {
				fmt.Fprintf(bw, "masi_control_operation_latency_seconds{operation=%q,quantile=%q} %.6f\n", operation, quantile, values[index])
			}
		}
	}
	return nil
}

func (m *Registry) writePostgresRuntimeMetrics(ctx context.Context, bw *bufio.Writer) error {
	var commits, rollbacks, deadlocks, tempBytes, blocksRead, blocksHit, walBytes int64
	var locks, replicationSenders int64
	err := m.pool.Pool.QueryRow(ctx, `SELECT d.xact_commit,d.xact_rollback,d.deadlocks,d.temp_bytes,
	 d.blks_read,d.blks_hit,w.wal_bytes,
	 (SELECT count(*) FROM pg_locks l WHERE l.database=(SELECT oid FROM pg_database WHERE datname=current_database())),
	 (SELECT count(*) FROM pg_stat_replication)
	 FROM pg_stat_database d CROSS JOIN pg_stat_wal w WHERE d.datname=current_database()`).
		Scan(&commits, &rollbacks, &deadlocks, &tempBytes, &blocksRead, &blocksHit, &walBytes, &locks, &replicationSenders)
	if err != nil {
		return err
	}
	fmt.Fprintln(bw, "# TYPE masi_control_postgresql_transactions_total counter")
	fmt.Fprintf(bw, "masi_control_postgresql_transactions_total{result=\"commit\"} %d\n", commits)
	fmt.Fprintf(bw, "masi_control_postgresql_transactions_total{result=\"rollback\"} %d\n", rollbacks)
	fmt.Fprintf(bw, "# TYPE masi_control_postgresql_deadlocks_total counter\nmasi_control_postgresql_deadlocks_total %d\n", deadlocks)
	fmt.Fprintf(bw, "# TYPE masi_control_postgresql_temp_bytes_total counter\nmasi_control_postgresql_temp_bytes_total %d\n", tempBytes)
	fmt.Fprintln(bw, "# TYPE masi_control_postgresql_blocks_total counter")
	fmt.Fprintf(bw, "masi_control_postgresql_blocks_total{kind=\"read\"} %d\n", blocksRead)
	fmt.Fprintf(bw, "masi_control_postgresql_blocks_total{kind=\"hit\"} %d\n", blocksHit)
	fmt.Fprintf(bw, "# TYPE masi_control_postgresql_wal_bytes_total counter\nmasi_control_postgresql_wal_bytes_total %d\n", walBytes)
	fmt.Fprintf(bw, "# TYPE masi_control_postgresql_locks gauge\nmasi_control_postgresql_locks %d\n", locks)
	fmt.Fprintf(bw, "# TYPE masi_control_postgresql_replication_senders gauge\nmasi_control_postgresql_replication_senders %d\n", replicationSenders)
	return nil
}

func processCPUSeconds() (float64, bool) {
	samples := []runtimemetrics.Sample{{Name: "/cpu/classes/total:cpu-seconds"}}
	runtimemetrics.Read(samples)
	if samples[0].Value.Kind() != runtimemetrics.KindFloat64 {
		return 0, false
	}
	return samples[0].Value.Float64(), true
}

func fileDescriptorCount() (int, bool) {
	entries, err := os.ReadDir("/proc/self/fd")
	if err != nil {
		return 0, false
	}
	return len(entries), true
}

func threadCount() (int, bool) {
	raw, err := os.ReadFile("/proc/self/status")
	if err != nil {
		return 0, false
	}
	for _, line := range strings.Split(string(raw), "\n") {
		if !strings.HasPrefix(line, "Threads:") {
			continue
		}
		value, err := strconv.Atoi(strings.TrimSpace(strings.TrimPrefix(line, "Threads:")))
		return value, err == nil
	}
	return 0, false
}

func residentBytes() (uint64, bool) {
	raw, err := os.ReadFile("/proc/self/statm")
	if err != nil {
		return 0, false
	}
	fields := strings.Fields(string(raw))
	if len(fields) < 2 {
		return 0, false
	}
	pages, err := strconv.ParseUint(fields[1], 10, 64)
	if err != nil {
		return 0, false
	}
	return pages * uint64(os.Getpagesize()), true
}
