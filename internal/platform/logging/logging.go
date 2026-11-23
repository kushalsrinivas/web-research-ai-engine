// Package logging builds the JSON logger used by every service.
package logging

import (
	"context"
	"log/slog"
	"os"
	"strings"

	"go.opentelemetry.io/otel/trace"

	"github.com/kushalsrinivas/web-research-ai-engine/internal/platform/config"
)

// New returns a JSON logger that stamps service identity and trace context on
// every record. The trace id is the join key between logs and traces.
func New(cfg config.Config) *slog.Logger {
	base := slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{
		Level:       cfg.LogLevel,
		ReplaceAttr: replaceAttr,
	})
	return slog.New(&traceHandler{next: base, service: cfg.Service, env: cfg.Env})
}

func replaceAttr(_ []string, attr slog.Attr) slog.Attr {
	switch attr.Key {
	case slog.TimeKey:
		attr.Key = "ts"
		attr.Value = slog.TimeValue(attr.Value.Time().UTC())
	case slog.LevelKey:
		attr.Value = slog.StringValue(strings.ToLower(attr.Value.String()))
	}
	return attr
}

type traceHandler struct {
	next    slog.Handler
	service string
	env     string
}

func (h *traceHandler) Enabled(ctx context.Context, level slog.Level) bool {
	return h.next.Enabled(ctx, level)
}

func (h *traceHandler) Handle(ctx context.Context, record slog.Record) error {
	record.AddAttrs(slog.String("service", h.service), slog.String("env", h.env))
	if span := trace.SpanContextFromContext(ctx); span.IsValid() {
		record.AddAttrs(
			slog.String("trace_id", span.TraceID().String()),
			slog.String("span_id", span.SpanID().String()),
		)
	}
	return h.next.Handle(ctx, record)
}

func (h *traceHandler) WithAttrs(attrs []slog.Attr) slog.Handler {
	return &traceHandler{next: h.next.WithAttrs(attrs), service: h.service, env: h.env}
}

func (h *traceHandler) WithGroup(name string) slog.Handler {
	return &traceHandler{next: h.next.WithGroup(name), service: h.service, env: h.env}
}
