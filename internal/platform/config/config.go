// Package config loads environment-driven configuration once at startup.
package config

import (
	"fmt"
	"log/slog"
	"os"
	"strconv"
	"strings"
	"time"
)

// Environment names used by the deployment docs.
const (
	Development = "development"
	Staging     = "staging"
	Production  = "production"
)

// Config is the process configuration. It contains no secrets beyond what the
// environment already provides; callers must not log it.
type Config struct {
	Service               string
	Env                   string
	Port                  int
	LogLevel              slog.Level
	SampleRatio           float64
	OTLPEndpoint          string
	DatabaseURL           string
	SourceUserAgent       string
	DiscoveryProvider     string
	KafkaBrokers          []string
	S3Endpoint            string
	S3AccessKey           string
	S3SecretKey           string
	S3BucketRaw           string
	S3Region              string
	SchedulerURL          string
	CrawlerUserAgent      string
	CrawlerAllowPrivate   bool
	CrawlerMaxDepth       int
	CrawlerMaxBodyBytes   int64
	FetcherMaxConcurrency int
	FetcherTimeout        time.Duration
	ShutdownTimeout       time.Duration
}

// Load reads and validates configuration. Required infrastructure settings are
// enforced outside development so a misconfigured staging/prod pod fails fast.
func Load(service string, defaultPort int) (Config, error) {
	cfg := Config{
		Service:               service,
		Env:                   env("APP_ENV", Development),
		Port:                  defaultPort,
		LogLevel:              slog.LevelInfo,
		OTLPEndpoint:          os.Getenv("OTEL_EXPORTER_OTLP_ENDPOINT"),
		DatabaseURL:           os.Getenv("DATABASE_URL"),
		SourceUserAgent:       env("SOURCE_USER_AGENT", "wre-source-registry/0.1 (+https://github.com/kushalsrinivas/web-research-ai-engine)"),
		DiscoveryProvider:     strings.ToLower(env("DISCOVERY_PROVIDER", "gdelt")),
		S3Endpoint:            os.Getenv("S3_ENDPOINT"),
		S3AccessKey:           os.Getenv("S3_ACCESS_KEY"),
		S3SecretKey:           os.Getenv("S3_SECRET_KEY"),
		S3BucketRaw:           env("S3_BUCKET_RAW", "wre-raw"),
		S3Region:              env("S3_REGION", "us-east-1"),
		SchedulerURL:          env("SCHEDULER_URL", "http://scheduler:8081"),
		CrawlerUserAgent:      env("CRAWLER_USER_AGENT", env("SOURCE_USER_AGENT", "wre-crawler/0.1 (+https://github.com/kushalsrinivas/web-research-ai-engine)")),
		CrawlerAllowPrivate:   false,
		CrawlerMaxDepth:       2,
		CrawlerMaxBodyBytes:   10 << 20,
		FetcherMaxConcurrency: 20,
		FetcherTimeout:        30 * time.Second,
		ShutdownTimeout:       10 * time.Second,
	}
	for _, broker := range strings.Split(os.Getenv("KAFKA_BROKERS"), ",") {
		if broker = strings.TrimSpace(broker); broker != "" {
			cfg.KafkaBrokers = append(cfg.KafkaBrokers, broker)
		}
	}

	switch cfg.Env {
	case Development, Staging, Production:
	default:
		return Config{}, fmt.Errorf("APP_ENV must be development, staging or production, got %q", cfg.Env)
	}

	if raw := os.Getenv("PORT"); raw != "" {
		port, err := strconv.Atoi(raw)
		if err != nil || port < 1 || port > 65535 {
			return Config{}, fmt.Errorf("PORT must be an integer in 1..65535, got %q", raw)
		}
		cfg.Port = port
	}

	switch strings.ToLower(env("LOG_LEVEL", "info")) {
	case "debug":
		cfg.LogLevel = slog.LevelDebug
	case "info":
		cfg.LogLevel = slog.LevelInfo
	case "warn":
		cfg.LogLevel = slog.LevelWarn
	case "error":
		cfg.LogLevel = slog.LevelError
	default:
		return Config{}, fmt.Errorf("LOG_LEVEL must be debug, info, warn or error")
	}

	cfg.SampleRatio = 1.0
	if cfg.Env != Development {
		cfg.SampleRatio = 0.1
	}
	if raw := os.Getenv("OTEL_SAMPLE_RATIO"); raw != "" {
		ratio, err := strconv.ParseFloat(raw, 64)
		if err != nil || ratio < 0 || ratio > 1 {
			return Config{}, fmt.Errorf("OTEL_SAMPLE_RATIO must be in 0..1, got %q", raw)
		}
		cfg.SampleRatio = ratio
	}

	if raw := os.Getenv("FETCHER_MAX_CONCURRENCY"); raw != "" {
		concurrency, err := strconv.Atoi(raw)
		if err != nil || concurrency < 1 || concurrency > 500 {
			return Config{}, fmt.Errorf("FETCHER_MAX_CONCURRENCY must be in 1..500, got %q", raw)
		}
		cfg.FetcherMaxConcurrency = concurrency
	}
	if raw := os.Getenv("FETCHER_TIMEOUT_SECONDS"); raw != "" {
		seconds, err := strconv.Atoi(raw)
		if err != nil || seconds < 1 || seconds > 300 {
			return Config{}, fmt.Errorf("FETCHER_TIMEOUT_SECONDS must be in 1..300, got %q", raw)
		}
		cfg.FetcherTimeout = time.Duration(seconds) * time.Second
	}

	cfg.CrawlerAllowPrivate = cfg.Env == Development
	if raw := strings.TrimSpace(os.Getenv("CRAWLER_ALLOW_PRIVATE")); raw != "" {
		allow, err := strconv.ParseBool(raw)
		if err != nil {
			return Config{}, fmt.Errorf("CRAWLER_ALLOW_PRIVATE must be true or false, got %q", raw)
		}
		cfg.CrawlerAllowPrivate = allow
	}
	if raw := os.Getenv("CRAWLER_MAX_DEPTH"); raw != "" {
		depth, err := strconv.Atoi(raw)
		if err != nil || depth < 0 || depth > 10 {
			return Config{}, fmt.Errorf("CRAWLER_MAX_DEPTH must be in 0..10, got %q", raw)
		}
		cfg.CrawlerMaxDepth = depth
	}
	if raw := os.Getenv("CRAWLER_MAX_BODY_MB"); raw != "" {
		megabytes, err := strconv.Atoi(raw)
		if err != nil || megabytes < 1 || megabytes > 100 {
			return Config{}, fmt.Errorf("CRAWLER_MAX_BODY_MB must be in 1..100, got %q", raw)
		}
		cfg.CrawlerMaxBodyBytes = int64(megabytes) << 20
	}

	if cfg.Env == Staging || cfg.Env == Production {
		for _, key := range []string{"DATABASE_URL", "KAFKA_BROKERS", "S3_ENDPOINT", "OTEL_EXPORTER_OTLP_ENDPOINT"} {
			if strings.TrimSpace(os.Getenv(key)) == "" {
				return Config{}, fmt.Errorf("%s is required when APP_ENV=%s", key, cfg.Env)
			}
		}
	}

	return cfg, nil
}

func env(key, fallback string) string {
	if value := strings.TrimSpace(os.Getenv(key)); value != "" {
		return value
	}
	return fallback
}
