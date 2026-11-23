"""OpenTelemetry tracing and propagation for Python services."""

from __future__ import annotations

from collections.abc import Callable

from opentelemetry import propagate, trace
from opentelemetry.baggage.propagation import W3CBaggagePropagator
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.aiohttp_server import AioHttpServerInstrumentor
from opentelemetry.propagators.composite import CompositePropagator
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from .config import Config


def init_telemetry(config: Config) -> Callable[[], None]:
    """Install the global tracer provider and return a shutdown callback."""
    resource = Resource.create(
        {
            "service.name": config.service,
            "service.version": config.processor_version,
            "deployment.environment": config.env,
        }
    )
    provider = TracerProvider(
        resource=resource,
        sampler=ParentBased(root=TraceIdRatioBased(config.sample_ratio)),
    )

    if config.otlp_endpoint:
        exporter = OTLPSpanExporter(
            endpoint=config.otlp_endpoint,
            insecure=not config.otlp_endpoint.startswith("https://"),
        )
        provider.add_span_processor(BatchSpanProcessor(exporter))

    trace.set_tracer_provider(provider)
    propagate.set_global_textmap(
        CompositePropagator([TraceContextTextMapPropagator(), W3CBaggagePropagator()])
    )

    instrumentor = AioHttpServerInstrumentor()
    instrumentor.instrument(tracer_provider=provider)

    def shutdown() -> None:
        instrumentor.uninstrument()
        provider.shutdown()

    return shutdown
