package sources

import (
	"context"
	"log/slog"
	"strings"
	"time"

	"github.com/prometheus/client_golang/prometheus"
)

// Metrics exposes aggregate source-registry health. Per-source cardinals are
// deliberately kept out of Prometheus; use the registry API for those.
type Metrics struct {
	sourcesByTypeStatus *prometheus.GaugeVec
	due                 prometheus.Gauge
	healthByState       *prometheus.GaugeVec
	reliabilityByType   *prometheus.GaugeVec
	discoveries         *prometheus.CounterVec
	crawlOutcomes       *prometheus.CounterVec
	scores              *prometheus.CounterVec
}

func NewMetrics(registry *prometheus.Registry) *Metrics {
	m := &Metrics{
		sourcesByTypeStatus: prometheus.NewGaugeVec(prometheus.GaugeOpts{
			Name: "wre_sources_total",
			Help: "Registered sources by type and status.",
		}, []string{"source_type", "status"}),
		due: prometheus.NewGauge(prometheus.GaugeOpts{
			Name: "wre_sources_due",
			Help: "Sources currently due for a crawl.",
		}),
		healthByState: prometheus.NewGaugeVec(prometheus.GaugeOpts{
			Name: "wre_source_health_total",
			Help: "Sources by current health state.",
		}, []string{"state"}),
		reliabilityByType: prometheus.NewGaugeVec(prometheus.GaugeOpts{
			Name: "wre_source_reliability_avg",
			Help: "Average persisted source-quality composite by source type.",
		}, []string{"source_type"}),
		discoveries: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "wre_source_discoveries_total",
			Help: "Discovery runs by mechanism and outcome.",
		}, []string{"mechanism", "outcome"}),
		crawlOutcomes: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "wre_source_crawl_outcomes_total",
			Help: "Reported crawl outcomes.",
		}, []string{"status"}),
		scores: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "wre_source_scores_total",
			Help: "Source-quality evaluations persisted.",
		}, []string{"model_version"}),
	}
	registry.MustRegister(m.sourcesByTypeStatus, m.due, m.healthByState, m.reliabilityByType,
		m.discoveries, m.crawlOutcomes, m.scores)
	return m
}

func (m *Metrics) ObserveDiscovery(mechanism, outcome string) {
	m.discoveries.WithLabelValues(mechanism, outcome).Inc()
}

func (m *Metrics) ObserveCrawlOutcome(status string) {
	m.crawlOutcomes.WithLabelValues(status).Inc()
}

func (m *Metrics) ObserveScore(modelVersion string) {
	m.scores.WithLabelValues(modelVersion).Inc()
}

func (m *Metrics) Refresh(ctx context.Context, repo *Repository) error {
	stats, err := repo.Stats(ctx)
	if err != nil {
		return err
	}
	m.sourcesByTypeStatus.Reset()
	for key, count := range stats.ByTypeStatus {
		parts := strings.SplitN(key, "|", 2)
		sourceType, status := "", ""
		if len(parts) == 2 {
			sourceType, status = parts[0], parts[1]
		}
		m.sourcesByTypeStatus.WithLabelValues(sourceType, status).Set(float64(count))
	}
	m.due.Set(float64(stats.Due))
	m.healthByState.Reset()
	for state, count := range stats.HealthByState {
		m.healthByState.WithLabelValues(state).Set(float64(count))
	}
	m.reliabilityByType.Reset()
	for sourceType, avg := range stats.ReliabilityByType {
		m.reliabilityByType.WithLabelValues(sourceType).Set(avg)
	}
	return nil
}

// StartRefresher refreshes aggregate metrics immediately and every interval
// until ctx is cancelled.
func (m *Metrics) StartRefresher(ctx context.Context, repo *Repository, logger *slog.Logger, interval time.Duration) {
	refresh := func() {
		refreshCtx, cancel := context.WithTimeout(ctx, 5*time.Second)
		defer cancel()
		if err := m.Refresh(refreshCtx, repo); err != nil {
			logger.Warn("source metrics refresh failed", "error", err)
		}
	}
	refresh()
	go func() {
		ticker := time.NewTicker(interval)
		defer ticker.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-ticker.C:
				refresh()
			}
		}
	}()
}
