package main

import (
	"context"
	"log"
	"log/slog"
	"time"

	"github.com/prometheus/client_golang/prometheus"

	"github.com/kushalsrinivas/web-research-ai-engine/internal/platform/config"
	"github.com/kushalsrinivas/web-research-ai-engine/internal/platform/db"
	"github.com/kushalsrinivas/web-research-ai-engine/internal/platform/health"
	"github.com/kushalsrinivas/web-research-ai-engine/internal/platform/service"
	"github.com/kushalsrinivas/web-research-ai-engine/internal/sources"
)

func main() {
	opts := service.Options{Setup: setup}
	if err := service.RunWithOptions("scheduler", 8081, opts); err != nil {
		log.Fatal(err)
	}
}

func setup(ctx context.Context, cfg config.Config, logger *slog.Logger, registry *prometheus.Registry) (service.SetupResult, error) {
	pool, err := db.Open(ctx, cfg.DatabaseURL)
	if err != nil {
		return service.SetupResult{}, err
	}

	provider, err := sources.NewTopicProvider(cfg.DiscoveryProvider, cfg.SourceUserAgent)
	if err != nil {
		pool.Close()
		return service.SetupResult{}, err
	}
	repo := sources.NewRepository(pool)
	discovery := sources.NewDiscovery(provider, cfg.SourceUserAgent, false)
	metrics := sources.NewMetrics(registry)
	metrics.StartRefresher(ctx, repo, logger, 30*time.Second)
	handler := sources.NewHandler(repo, discovery, metrics, logger)

	logger.InfoContext(ctx, "source registry ready", "topic_provider", cfg.DiscoveryProvider)
	return service.SetupResult{
		Checks: []health.Check{{
			Name: "postgres",
			Fn:   pool.Ping,
		}},
		Mount: handler.Mount,
		Close: pool.Close,
	}, nil
}
