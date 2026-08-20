package metrics

import (
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestHTTPMetricsAreBoundedAndExposeLatencyBytesAndDrift(t *testing.T) {
	dir := t.TempDir()
	configPath := filepath.Join(dir, "control.json")
	if err := os.WriteFile(configPath, []byte(`{"profile":"test"}`), 0o600); err != nil {
		t.Fatal(err)
	}
	registry := New(nil)
	if err := registry.ConfigureRuntimeFiles(configPath, "", ""); err != nil {
		t.Fatal(err)
	}
	handler := registry.Middleware(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusCreated)
		w.WriteHeader(http.StatusInternalServerError)
		_, _ = w.Write([]byte("ok!"))
	}))
	req := httptest.NewRequest(http.MethodPost, "/api/effects/proposals", strings.NewReader("body"))
	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, req)
	if rec.Code != http.StatusCreated {
		t.Fatalf("first status must win: %d", rec.Code)
	}

	scrape := httptest.NewRecorder()
	registry.Handler(scrape, httptest.NewRequest(http.MethodGet, "/metrics", nil))
	body := scrape.Body.String()
	for _, expected := range []string{
		`masi_control_http_requests_total{route_group="api_effects",method="POST",status_class="2xx"} 1`,
		`masi_control_http_request_bytes_total{route_group="api_effects",method="POST",status_class="2xx"} 4`,
		`masi_control_http_response_bytes_total{route_group="api_effects",method="POST",status_class="2xx"} 3`,
		`masi_control_http_request_duration_seconds_bucket`,
		`le="+Inf"} 1`,
		`masi_control_metrics_active_series_limit 5000`,
		`masi_control_config_drift 0`,
		`masi_control_certificate_read_error{endpoint="http"} 1`,
	} {
		if !strings.Contains(body, expected) {
			t.Fatalf("metrics missing %q:\n%s", expected, body)
		}
	}
	if strings.Contains(body, configPath) || strings.Contains(body, "profile") {
		t.Fatal("config identity/content must not become a metric label")
	}
	if err := os.WriteFile(configPath, []byte(`{"profile":"changed"}`), 0o600); err != nil {
		t.Fatal(err)
	}
	drift := httptest.NewRecorder()
	registry.Handler(drift, httptest.NewRequest(http.MethodGet, "/metrics", nil))
	if !strings.Contains(drift.Body.String(), "masi_control_config_drift 1") {
		t.Fatalf("config drift not detected: %s", drift.Body.String())
	}
}

func TestMetricDimensionsCollapseUnknownInputs(t *testing.T) {
	if routeGroup("/api/unknown/object-id-123") != "api_other" || routeGroup("/totally/arbitrary") != "other" {
		t.Fatal("arbitrary paths must collapse to fixed route groups")
	}
	if boundedMethod("PATCH") != "OTHER" || statusClass(999) != "invalid" {
		t.Fatal("unknown method/status dimensions must collapse")
	}
}
