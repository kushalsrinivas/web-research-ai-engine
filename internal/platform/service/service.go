// Package service is the shared bootstrap for Go services. Each cmd main only
// declares its name, port, readiness checks, and optional service routes.
package service

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"strconv"
	"syscall"
	"time"

	"github.com/go-chi/chi/v5"
	"github.com/prometheus/client_golang/prometheus"
	"go.opentelemetry.io/contrib/instrumentation/net/http/otelhttp"

	"github.com/kushalsrinivas/web-research-ai-engine/internal/platform/config"
	"github.com/kushalsrinivas/web-research-ai-engine/internal/platform/health"
	"github.com/kushalsrinivas/web-research-ai-engine/internal/platform/logging"
	"github.com/kushalsrinivas/web-research-ai-engine/internal/platform/telemetry"
	"github.com/kushalsrinivas/web-research-ai-engine/internal/platform/version"
)

// SetupResult is what a service-specific setup contributes to the shared
// server. Close runs after the HTTP server stops.
type SetupResult struct {
	Checks []health.Check
	Mount  func(chi.Router)
	Close  func()
}

// Options lets a service add readiness checks and routes after config and
// telemetry are initialized.
type Options struct {
	Setup func(context.Context, config.Config, *slog.Logger, *prometheus.Registry) (SetupResult, error)
}

// Run starts the probe server and blocks until SIGINT/SIGTERM.
//
// Passing "healthcheck" as the only argument instead probes the local
// /healthz endpoint and exits, which lets distroless images use a Docker
// healthcheck without a shell or curl.
func Run(service string, defaultPort int, checks []health.Check) error {
	return RunWithOptions(service, defaultPort, Options{
		Setup: func(context.Context, config.Config, *slog.Logger, *prometheus.Registry) (SetupResult, error) {
			return SetupResult{Checks: checks}, nil
		},
	})
}

// RunWithOptions is Run with a service-specific setup callback.
func RunWithOptions(service string, defaultPort int, opts Options) error {
	if len(os.Args) == 2 && os.Args[1] == "healthcheck" {
		return probe(defaultPort)
	}

	cfg, err := config.Load(service, defaultPort)
	if err != nil {
		return err
	}
	logger := logging.New(cfg)

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	telemetryProvider, err := telemetry.Init(ctx, cfg)
	if err != nil {
		return err
	}
	defer func() {
		shutdownCtx, cancel := context.WithTimeout(context.Background(), cfg.ShutdownTimeout)
		defer cancel()
		if err := telemetryProvider.Shutdown(shutdownCtx); err != nil {
			logger.Error("telemetry shutdown failed", "error", err)
		}
	}()

	var setup SetupResult
	if opts.Setup != nil {
		setup, err = opts.Setup(ctx, cfg, logger, telemetryProvider.Registry)
		if err != nil {
			return err
		}
	}
	if setup.Close != nil {
		defer setup.Close()
	}

	router := health.New(service, version.String(service), setup.Checks, telemetryProvider.Registry, logger)
	if setup.Mount != nil {
		setup.Mount(router)
	}

	srv := &http.Server{
		Addr:              fmt.Sprintf(":%d", cfg.Port),
		Handler:           otelhttp.NewHandler(router, service),
		ReadHeaderTimeout: 5 * time.Second,
		IdleTimeout:       60 * time.Second,
	}

	serverErr := make(chan error, 1)
	go func() {
		logger.InfoContext(ctx, "service listening",
			"addr", srv.Addr,
			"version", version.String(service),
			"env", cfg.Env,
		)
		serverErr <- srv.ListenAndServe()
	}()

	select {
	case err := <-serverErr:
		if !errors.Is(err, http.ErrServerClosed) {
			return fmt.Errorf("http server: %w", err)
		}
	case <-ctx.Done():
		logger.Info("shutting down")
	}

	shutdownCtx, cancel := context.WithTimeout(context.Background(), cfg.ShutdownTimeout)
	defer cancel()
	if err := srv.Shutdown(shutdownCtx); err != nil {
		return fmt.Errorf("shutdown: %w", err)
	}
	return nil
}

func probe(defaultPort int) error {
	port := defaultPort
	if raw := os.Getenv("PORT"); raw != "" {
		parsed, err := strconv.Atoi(raw)
		if err != nil {
			return fmt.Errorf("invalid PORT %q: %w", raw, err)
		}
		port = parsed
	}

	client := &http.Client{Timeout: 2 * time.Second}
	resp, err := client.Get(fmt.Sprintf("http://127.0.0.1:%d/healthz", port))
	if err != nil {
		return err
	}
	defer func() { _ = resp.Body.Close() }()
	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("healthcheck returned %d", resp.StatusCode)
	}
	return nil
}
