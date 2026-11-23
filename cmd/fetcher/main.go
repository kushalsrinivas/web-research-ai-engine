package main

import (
	"context"
	"log"
	"log/slog"
	"time"

	"github.com/prometheus/client_golang/prometheus"

	"github.com/kushalsrinivas/web-research-ai-engine/internal/crawler/archive"
	"github.com/kushalsrinivas/web-research-ai-engine/internal/crawler/fetcher"
	"github.com/kushalsrinivas/web-research-ai-engine/internal/crawler/metrics"
	"github.com/kushalsrinivas/web-research-ai-engine/internal/crawler/robots"
	"github.com/kushalsrinivas/web-research-ai-engine/internal/crawler/safehttp"
	"github.com/kushalsrinivas/web-research-ai-engine/internal/crawler/store"
	"github.com/kushalsrinivas/web-research-ai-engine/internal/crawler/worker"
	"github.com/kushalsrinivas/web-research-ai-engine/internal/platform/config"
	"github.com/kushalsrinivas/web-research-ai-engine/internal/platform/db"
	"github.com/kushalsrinivas/web-research-ai-engine/internal/platform/health"
	"github.com/kushalsrinivas/web-research-ai-engine/internal/platform/service"
)

func main() {
	if err := service.RunWithOptions("fetcher", 8082, service.Options{Setup: setup}); err != nil {
		log.Fatal(err)
	}
}

func setup(ctx context.Context, cfg config.Config, logger *slog.Logger, registry *prometheus.Registry) (service.SetupResult, error) {
	pool, err := db.Open(ctx, cfg.DatabaseURL)
	if err != nil {
		return service.SetupResult{}, err
	}

	consumer, err := worker.NewConsumer(cfg.KafkaBrokers, cfg.FetcherMaxConcurrency)
	if err != nil {
		pool.Close()
		return service.SetupResult{}, err
	}
	archiveStore, err := archive.NewS3(ctx, archive.Config{
		Endpoint:  cfg.S3Endpoint,
		AccessKey: cfg.S3AccessKey,
		SecretKey: cfg.S3SecretKey,
		Bucket:    cfg.S3BucketRaw,
		Region:    cfg.S3Region,
	})
	if err != nil {
		consumer.Close()
		pool.Close()
		return service.SetupResult{}, err
	}

	st := store.New(pool)
	robotsClient := safehttp.NewClient(cfg.FetcherTimeout, cfg.CrawlerAllowPrivate)
	workerInstance := worker.New(worker.Config{
		WorkerID:          "",
		Concurrency:       cfg.FetcherMaxConcurrency,
		MaxDepth:          cfg.CrawlerMaxDepth,
		UserAgent:         cfg.CrawlerUserAgent,
		AllowPrivate:      cfg.CrawlerAllowPrivate,
		FetchTimeout:      cfg.FetcherTimeout,
		MaxBodyBytes:      cfg.CrawlerMaxBodyBytes,
		HeartbeatInterval: 10 * time.Second,
		SchedulerURL:      cfg.SchedulerURL,
	}, st, consumer,
		robots.NewService(pool, robotsClient, cfg.CrawlerUserAgent),
		fetcher.New(fetcher.Config{
			Timeout:      cfg.FetcherTimeout,
			MaxBodyBytes: cfg.CrawlerMaxBodyBytes,
			UserAgent:    cfg.CrawlerUserAgent,
			AllowPrivate: cfg.CrawlerAllowPrivate,
		}),
		archiveStore, metrics.New(registry), logger)

	done := make(chan struct{})
	go func() {
		defer close(done)
		if err := workerInstance.Run(ctx); err != nil && ctx.Err() == nil {
			logger.Error("crawler worker stopped", "error", err)
		}
	}()
	logger.InfoContext(ctx, "crawler worker ready", "concurrency", cfg.FetcherMaxConcurrency,
		"allow_private", cfg.CrawlerAllowPrivate, "max_depth", cfg.CrawlerMaxDepth)

	return service.SetupResult{
		Checks: []health.Check{
			{Name: "postgres", Fn: pool.Ping},
			{Name: "kafka", Fn: func(checkCtx context.Context) error { return consumer.Ping(checkCtx) }},
		},
		Close: func() {
			select {
			case <-done:
			case <-time.After(15 * time.Second):
				logger.Warn("crawler worker did not stop in time")
			}
			consumer.Close()
			pool.Close()
		},
	}, nil
}
