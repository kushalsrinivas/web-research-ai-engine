// Package telemetry initializes tracing, metrics, and propagation for a service.
package telemetry

import (
	"context"
	"fmt"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/collectors"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracegrpc"
	"go.opentelemetry.io/otel/propagation"
	"go.opentelemetry.io/otel/sdk/resource"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"

	"github.com/kushalsrinivas/web-research-ai-engine/internal/platform/config"
	"github.com/kushalsrinivas/web-research-ai-engine/internal/platform/version"
)

// Provider owns the process-wide tracing and metrics state.
type Provider struct {
	Registry *prometheus.Registry
	tracer   *sdktrace.TracerProvider
}

// Init installs the global tracer provider and propagator, and creates the
// process metric registry. An empty OTLP endpoint keeps tracing local (useful
// for tests and `go run`) instead of failing startup.
func Init(ctx context.Context, cfg config.Config) (*Provider, error) {
	res, err := resource.New(ctx,
		resource.WithTelemetrySDK(),
		resource.WithProcess(),
		resource.WithAttributes(
			attribute.String("service.name", cfg.Service),
			attribute.String("service.version", version.String(cfg.Service)),
			attribute.String("deployment.environment", cfg.Env),
		),
	)
	if err != nil {
		return nil, fmt.Errorf("build otel resource: %w", err)
	}

	opts := []sdktrace.TracerProviderOption{
		sdktrace.WithResource(res),
		sdktrace.WithSampler(sdktrace.ParentBased(sdktrace.TraceIDRatioBased(cfg.SampleRatio))),
	}
	if cfg.OTLPEndpoint != "" {
		exporter, err := otlptracegrpc.New(ctx,
			otlptracegrpc.WithEndpointURL(cfg.OTLPEndpoint),
			otlptracegrpc.WithInsecure(),
		)
		if err != nil {
			return nil, fmt.Errorf("create otlp exporter: %w", err)
		}
		opts = append(opts, sdktrace.WithBatcher(exporter))
	}

	tracer := sdktrace.NewTracerProvider(opts...)
	otel.SetTracerProvider(tracer)
	otel.SetTextMapPropagator(propagation.NewCompositeTextMapPropagator(
		propagation.TraceContext{},
		propagation.Baggage{},
	))

	registry := prometheus.NewRegistry()
	registry.MustRegister(
		collectors.NewGoCollector(),
		collectors.NewProcessCollector(collectors.ProcessCollectorOpts{}),
	)
	up := prometheus.NewGaugeVec(prometheus.GaugeOpts{
		Name: "wre_service_up",
		Help: "1 while the service process is running.",
	}, []string{"version"})
	up.WithLabelValues(version.String(cfg.Service)).Set(1)
	registry.MustRegister(up)

	return &Provider{Registry: registry, tracer: tracer}, nil
}

// Shutdown flushes pending spans. Call it after the HTTP server stops.
func (p *Provider) Shutdown(ctx context.Context) error {
	if p == nil || p.tracer == nil {
		return nil
	}
	return p.tracer.Shutdown(ctx)
}
