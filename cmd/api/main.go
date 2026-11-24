package main

import (
	"context"
	"io"
	"log"
	"log/slog"
	"net/http"

	"github.com/go-chi/chi/v5"
	"github.com/prometheus/client_golang/prometheus"

	"github.com/kushalsrinivas/web-research-ai-engine/internal/platform/config"
	"github.com/kushalsrinivas/web-research-ai-engine/internal/platform/service"
)

// fixtureArticle is deterministic content for local smoke tests. It lets the
// crawler and ETL exercise HTML extraction without depending on the internet.
const fixtureArticle = `<!doctype html>
<html><head>
<meta charset="utf-8">
<title>Gujarat semiconductor investment announced</title>
<meta name="author" content="Fixture Desk">
<meta property="article:published_time" content="2026-11-20T08:00:00Z">
<link rel="canonical" href="https://example.com/semiconductors/gujarat-fixture">
</head><body>
<article>
<h1>Gujarat semiconductor investment announced</h1>
<p>Tata Electronics announced plans to invest ₹15,000 crore in a
semiconductor facility in Gujarat. The facility is expected to begin production in 2028,
subject to regulatory approvals and the availability of water and power infrastructure.</p>
<p>The Ministry of Electronics and Information Technology approved the project under the
semiconductor policy. Officials said the project would create several thousand direct and
indirect jobs and support the local supplier base over the next decade.</p>
<p>Analysts cautioned that an announced investment is not the same as committed capital
spending. Final investment decisions often depend on incentive packages, customer
commitments, and the pace of equipment deliveries.</p>
</article>
</body></html>`

func main() {
	if err := service.RunWithOptions("api", 8080, service.Options{Setup: setup}); err != nil {
		log.Fatal(err)
	}
}

func setup(_ context.Context, cfg config.Config, _ *slog.Logger, _ *prometheus.Registry) (service.SetupResult, error) {
	if cfg.Env != config.Development {
		return service.SetupResult{}, nil
	}
	return service.SetupResult{
		Mount: func(r chi.Router) {
			r.Get("/internal/v1/fixtures/article", func(w http.ResponseWriter, _ *http.Request) {
				w.Header().Set("Content-Type", "text/html; charset=utf-8")
				w.Header().Set("Cache-Control", "no-store")
				_, _ = io.WriteString(w, fixtureArticle)
			})
		},
	}, nil
}
