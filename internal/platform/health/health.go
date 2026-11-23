// Package health exposes the probe-only HTTP surface every service must have:
// liveness, readiness, and Prometheus metrics.
package health

import (
	"context"
	"encoding/json"
	"log/slog"
	"net/http"
	"time"

	"github.com/go-chi/chi/v5"
	"github.com/go-chi/chi/v5/middleware"
	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promhttp"
)

// Check is a named readiness dependency.
type Check struct {
	Name string
	Fn   func(context.Context) error
}

// New builds the probe router. checks may be empty while a service has no
// dependencies wired yet. Callers may mount service routes on the returned
// router before serving it.
func New(service, processorVersion string, checks []Check, registry *prometheus.Registry, logger *slog.Logger) *chi.Mux {
	if registry == nil {
		registry = prometheus.NewRegistry()
	}

	requests := prometheus.NewCounterVec(prometheus.CounterOpts{
		Name: "wre_http_requests_total",
		Help: "HTTP requests handled, by route, method and status class.",
	}, []string{"route", "method", "status_class"})
	duration := prometheus.NewHistogramVec(prometheus.HistogramOpts{
		Name:    "wre_http_request_duration_seconds",
		Help:    "HTTP request latency, by route and method.",
		Buckets: prometheus.DefBuckets,
	}, []string{"route", "method"})
	registry.MustRegister(requests, duration)

	h := &handler{
		service:     service,
		version:     processorVersion,
		checks:      checks,
		logger:      logger,
		requests:    requests,
		duration:    duration,
		metrics:     promhttp.HandlerFor(registry, promhttp.HandlerOpts{}),
		metricsPath: "/metrics",
	}

	r := chi.NewRouter()
	r.Use(middleware.Recoverer)
	r.Get("/healthz", h.instrument("/healthz", h.healthz))
	r.Get("/readyz", h.instrument("/readyz", h.readyz))
	r.Handle(h.metricsPath, h.metrics)
	return r
}

type handler struct {
	service     string
	version     string
	checks      []Check
	logger      *slog.Logger
	requests    *prometheus.CounterVec
	duration    *prometheus.HistogramVec
	metrics     http.Handler
	metricsPath string
}

func (h *handler) healthz(w http.ResponseWriter, r *http.Request) {
	h.writeJSON(w, http.StatusOK, map[string]any{
		"status":  "ok",
		"service": h.service,
		"version": h.version,
	})
}

func (h *handler) readyz(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := context.WithTimeout(r.Context(), 2*time.Second)
	defer cancel()

	results := make(map[string]string, len(h.checks))
	ready := true
	for _, check := range h.checks {
		if err := check.Fn(ctx); err != nil {
			results[check.Name] = "error"
			ready = false
			continue
		}
		results[check.Name] = "ok"
	}

	status := http.StatusOK
	state := "ok"
	if !ready {
		status = http.StatusServiceUnavailable
		state = "unavailable"
	}
	h.writeJSON(w, status, map[string]any{
		"status":  state,
		"service": h.service,
		"version": h.version,
		"checks":  results,
	})
}

func (h *handler) writeJSON(w http.ResponseWriter, status int, body any) {
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Cache-Control", "no-store")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(body)
}

func (h *handler) instrument(route string, next http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		recorder := &statusRecorder{ResponseWriter: w, status: http.StatusOK}
		next(recorder, r)
		elapsed := time.Since(start)

		class := statusClass(recorder.status)
		h.requests.WithLabelValues(route, r.Method, class).Inc()
		h.duration.WithLabelValues(route, r.Method).Observe(elapsed.Seconds())
		if h.logger != nil {
			h.logger.InfoContext(r.Context(), "http request",
				"route", route,
				"method", r.Method,
				"status", recorder.status,
				"duration_ms", elapsed.Milliseconds(),
			)
		}
	}
}

type statusRecorder struct {
	http.ResponseWriter
	status int
}

func (r *statusRecorder) WriteHeader(status int) {
	r.status = status
	r.ResponseWriter.WriteHeader(status)
}

func (r *statusRecorder) Unwrap() http.ResponseWriter {
	return r.ResponseWriter
}

func statusClass(status int) string {
	switch {
	case status >= 500:
		return "5xx"
	case status >= 400:
		return "4xx"
	case status >= 300:
		return "3xx"
	default:
		return "2xx"
	}
}
