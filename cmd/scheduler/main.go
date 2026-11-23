package main

import (
	"context"
	"log"
	"log/slog"
	"time"

	"github.com/go-chi/chi/v5"
	"github.com/prometheus/client_golang/prometheus"
	"github.com/twmb/franz-go/pkg/kgo"

	"github.com/kushalsrinivas/web-research-ai-engine/internal/crawler/dispatch"
	crawlmetrics "github.com/kushalsrinivas/web-research-ai-engine/internal/crawler/metrics"
	"github.com/kushalsrinivas/web-research-ai-engine/internal/crawler/store"
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

	crawlStore := store.New(pool)
	dispatcher, producer, err := buildDispatcher(cfg, crawlStore, repo, crawlmetrics.New(registry), logger)
	if err != nil {
		pool.Close()
		return service.SetupResult{}, err
	}
	if dispatcher != nil {
		go dispatcher.Run(ctx)
		logger.InfoContext(ctx, "crawl dispatcher ready", "brokers", cfg.KafkaBrokers)
	} else {
		logger.WarnContext(ctx, "KAFKA_BROKERS is empty; crawl dispatch is disabled")
	}

	checks := []health.Check{{Name: "postgres", Fn: pool.Ping}}
	if producer != nil {
		checks = append(checks, health.Check{
			Name: "kafka",
			Fn:   func(checkCtx context.Context) error { return producer.Ping(checkCtx) },
		})
	}

	logger.InfoContext(ctx, "source registry ready", "topic_provider", cfg.DiscoveryProvider)
	return service.SetupResult{
		Checks: checks,
		Mount: func(r chi.Router) {
			handler.Mount(r)
			if dispatcher != nil {
				dispatcher.Mount(r)
			}
		},
		Close: func() {
			if producer != nil {
				producer.Close()
			}
			pool.Close()
		},
	}, nil
}

func buildDispatcher(cfg config.Config, crawlStore *store.Store,
	repo *sources.Repository, metrics *crawlmetrics.Metrics, logger *slog.Logger) (*dispatch.Dispatcher, *kgo.Client, error) {
	if len(cfg.KafkaBrokers) == 0 {
		return nil, nil, nil
	}
	producer, err := kgo.NewClient(
		kgo.SeedBrokers(cfg.KafkaBrokers...),
		kgo.ProduceRequestTimeout(10*time.Second),
		kgo.MaxBufferedRecords(1000),
		kgo.AllowAutoTopicCreation(),
	)
	if err != nil {
		return nil, nil, err
	}
	return dispatch.New(crawlStore, repo, producer, metrics, logger, dispatch.DefaultConfig()), producer, nil
}
