package health

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/prometheus/client_golang/prometheus"
)

func TestHealthz(t *testing.T) {
	srv := httptest.NewServer(New("api", "api@dev", nil, prometheus.NewRegistry(), nil))
	defer srv.Close()

	resp, err := http.Get(srv.URL + "/healthz")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status = %d, want 200", resp.StatusCode)
	}
	var body map[string]any
	if err := json.NewDecoder(resp.Body).Decode(&body); err != nil {
		t.Fatal(err)
	}
	if body["status"] != "ok" || body["service"] != "api" {
		t.Fatalf("unexpected body: %#v", body)
	}
}

func TestReadyzReportsFailures(t *testing.T) {
	checks := []Check{{
		Name: "database",
		Fn:   func(context.Context) error { return errors.New("down") },
	}}
	srv := httptest.NewServer(New("api", "api@dev", checks, prometheus.NewRegistry(), nil))
	defer srv.Close()

	resp, err := http.Get(srv.URL + "/readyz")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusServiceUnavailable {
		t.Fatalf("status = %d, want 503", resp.StatusCode)
	}
	var body struct {
		Status string            `json:"status"`
		Checks map[string]string `json:"checks"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&body); err != nil {
		t.Fatal(err)
	}
	if body.Status != "unavailable" || body.Checks["database"] != "error" {
		t.Fatalf("unexpected body: %#v", body)
	}
}

func TestMetricsIncludeRequestCounts(t *testing.T) {
	registry := prometheus.NewRegistry()
	srv := httptest.NewServer(New("api", "api@dev", nil, registry, nil))
	defer srv.Close()

	resp, err := http.Get(srv.URL + "/healthz")
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()

	resp, err = http.Get(srv.URL + "/metrics")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var buf bytes.Buffer
	if _, err := buf.ReadFrom(resp.Body); err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(buf.String(), `wre_http_requests_total{method="GET",route="/healthz",status_class="2xx"} 1`) {
		t.Fatalf("request metric missing:\n%s", buf.String())
	}
}
