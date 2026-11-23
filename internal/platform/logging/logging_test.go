package logging

import (
	"bytes"
	"context"
	"encoding/json"
	"log/slog"
	"testing"
	"time"

	"go.opentelemetry.io/otel/trace"
)

func TestReplaceAttrNormalizesTimeToUTC(t *testing.T) {
	ist := time.FixedZone("IST", 5*60*60+30*60)
	attr := replaceAttr(nil, slog.Attr{
		Key:   slog.TimeKey,
		Value: slog.TimeValue(time.Date(2026, 11, 23, 9, 0, 0, 0, ist)),
	})
	if attr.Key != "ts" {
		t.Fatalf("key = %q, want ts", attr.Key)
	}
	if got := attr.Value.Time().Format(time.RFC3339); got != "2026-11-23T03:30:00Z" {
		t.Fatalf("time = %s, want UTC", got)
	}
}

func TestTraceHandlerAddsServiceAndTraceContext(t *testing.T) {
	var buf bytes.Buffer
	logger := slog.New(&traceHandler{
		next:    slog.NewJSONHandler(&buf, nil),
		service: "parser",
		env:     "development",
	})

	traceID, _ := trace.TraceIDFromHex("4bf92f3577b34da6a3ce929d0e0e4736")
	spanID, _ := trace.SpanIDFromHex("00f067aa0ba902b7")
	ctx := trace.ContextWithSpanContext(context.Background(), trace.NewSpanContext(trace.SpanContextConfig{
		TraceID: traceID,
		SpanID:  spanID,
	}))

	logger.InfoContext(ctx, "unit of work", "document_id", "01JB2Q1H7S5D9F4K2M8N6P3R0T")

	var got map[string]any
	if err := json.Unmarshal(buf.Bytes(), &got); err != nil {
		t.Fatalf("log is not JSON: %v\n%s", err, buf.String())
	}
	for key, want := range map[string]string{
		"service":  "parser",
		"env":      "development",
		"trace_id": "4bf92f3577b34da6a3ce929d0e0e4736",
		"span_id":  "00f067aa0ba902b7",
		"msg":      "unit of work",
	} {
		if got[key] != want {
			t.Fatalf("%s = %v, want %s (log: %s)", key, got[key], want, buf.String())
		}
	}
}
