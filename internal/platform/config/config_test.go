package config

import (
	"log/slog"
	"testing"
)

func TestLoadDefaults(t *testing.T) {
	t.Setenv("APP_ENV", "")
	t.Setenv("PORT", "")
	t.Setenv("LOG_LEVEL", "")
	t.Setenv("OTEL_SAMPLE_RATIO", "")

	cfg, err := Load("api", 8080)
	if err != nil {
		t.Fatalf("Load() error = %v", err)
	}
	if cfg.Env != Development || cfg.Port != 8080 {
		t.Fatalf("unexpected defaults: %+v", cfg)
	}
	if cfg.LogLevel != slog.LevelInfo || cfg.SampleRatio != 1.0 {
		t.Fatalf("unexpected logging defaults: %+v", cfg)
	}
}

func TestLoadOverrides(t *testing.T) {
	t.Setenv("APP_ENV", "development")
	t.Setenv("PORT", "9090")
	t.Setenv("LOG_LEVEL", "debug")
	t.Setenv("OTEL_SAMPLE_RATIO", "0.25")

	cfg, err := Load("api", 8080)
	if err != nil {
		t.Fatalf("Load() error = %v", err)
	}
	if cfg.Port != 9090 || cfg.LogLevel != slog.LevelDebug || cfg.SampleRatio != 0.25 {
		t.Fatalf("overrides not applied: %+v", cfg)
	}
}

func TestLoadRejectsBadValues(t *testing.T) {
	cases := []struct {
		name string
		key  string
		val  string
	}{
		{"environment", "APP_ENV", "qa"},
		{"port", "PORT", "not-a-port"},
		{"port range", "PORT", "70000"},
		{"log level", "LOG_LEVEL", "verbose"},
		{"sample ratio", "OTEL_SAMPLE_RATIO", "2"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Setenv("APP_ENV", "development")
			t.Setenv("PORT", "")
			t.Setenv("LOG_LEVEL", "")
			t.Setenv("OTEL_SAMPLE_RATIO", "")
			t.Setenv(tc.key, tc.val)
			if _, err := Load("api", 8080); err == nil {
				t.Fatalf("expected error for %s=%q", tc.key, tc.val)
			}
		})
	}
}

func TestLoadRequiresInfrastructureOutsideDevelopment(t *testing.T) {
	t.Setenv("APP_ENV", "staging")
	t.Setenv("DATABASE_URL", "")
	t.Setenv("KAFKA_BROKERS", "")
	t.Setenv("S3_ENDPOINT", "")
	t.Setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "")

	if _, err := Load("api", 8080); err == nil {
		t.Fatal("expected staging configuration to fail without required settings")
	}
}
