package metrics

import (
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"testing"
	"time"

	"context"

	"masi-nids/control-go/internal/config"
	"masi-nids/control-go/internal/db"
)

func TestMetricsCanonicalAggregatesPostgres(t *testing.T) {
	configPath := os.Getenv("MASI_CONTROL_E2E_CONFIG")
	if configPath == "" || os.Getenv("MASI_CONTROL_E2E_DSN") == "" {
		if os.Getenv("MASI_CONTROL_E2E_REQUIRED") == "1" {
			t.Fatal("metrics PostgreSQL E2E required but DSN/CONFIG is missing")
		}
		t.Skip("set MASI_CONTROL_E2E_DSN/CONFIG for metrics PostgreSQL E2E")
	}
	cfg, err := config.Load(configPath)
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	pool, err := db.New(ctx, cfg)
	if err != nil {
		t.Fatal(err)
	}
	defer pool.Close()
	registry := New(pool)
	if err := registry.ConfigureRuntimeFiles(configPath, "", ""); err != nil {
		t.Fatal(err)
	}
	rec := httptest.NewRecorder()
	registry.Handler(rec, httptest.NewRequest(http.MethodGet, "/metrics", nil))
	body := rec.Body.String()
	for _, expected := range []string{
		"masi_control_metrics_postgresql_scrape_error 0",
		"masi_control_effect_intents{state=\"unknown\"}",
		"masi_control_firewall_bindings{state=\"reconciling\"}",
		"masi_control_analysis_tasks{outcome=\"limited\"}",
		"masi_control_operation_latency_seconds{operation=",
		"masi_control_postgresql_wal_bytes_total",
	} {
		if !strings.Contains(body, expected) {
			t.Fatalf("metrics query missing %q or failed:\n%s", expected, body)
		}
	}
}
