// Package metrics exposes crawler counters and latency.
package metrics

import (
	"time"

	"github.com/prometheus/client_golang/prometheus"
)

type Metrics struct {
	jobs           *prometheus.CounterVec
	fetchTime      *prometheus.HistogramVec
	bytes          *prometheus.CounterVec
	inflight       prometheus.Gauge
	retries        prometheus.Counter
	deadLetter     prometheus.Counter
	leaseRecovered prometheus.Counter
	robotsDenied   prometheus.Counter
	links          prometheus.Counter
}

func New(registry *prometheus.Registry) *Metrics {
	m := &Metrics{
		jobs: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "wre_crawler_jobs_total",
			Help: "Crawl jobs finished, by status.",
		}, []string{"status"}),
		fetchTime: prometheus.NewHistogramVec(prometheus.HistogramOpts{
			Name:    "wre_crawler_fetch_duration_seconds",
			Help:    "Fetch duration, by content kind and outcome.",
			Buckets: []float64{0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60},
		}, []string{"kind", "outcome"}),
		bytes: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "wre_crawler_bytes_total",
			Help: "Bytes archived, by content kind.",
		}, []string{"kind"}),
		inflight: prometheus.NewGauge(prometheus.GaugeOpts{
			Name: "wre_crawler_inflight",
			Help: "Jobs currently being fetched.",
		}),
		retries: prometheus.NewCounter(prometheus.CounterOpts{
			Name: "wre_crawler_retries_total",
			Help: "Jobs scheduled for another attempt.",
		}),
		deadLetter: prometheus.NewCounter(prometheus.CounterOpts{
			Name: "wre_crawler_dead_letter_total",
			Help: "Jobs that exhausted their retry budget.",
		}),
		leaseRecovered: prometheus.NewCounter(prometheus.CounterOpts{
			Name: "wre_crawler_lease_recovered_total",
			Help: "Jobs recovered after a worker stopped heartbeating.",
		}),
		robotsDenied: prometheus.NewCounter(prometheus.CounterOpts{
			Name: "wre_crawler_robots_denied_total",
			Help: "URLs skipped by robots.txt or a domain policy.",
		}),
		links: prometheus.NewCounter(prometheus.CounterOpts{
			Name: "wre_crawler_links_discovered_total",
			Help: "Follow-up URLs discovered in fetched documents.",
		}),
	}
	registry.MustRegister(m.jobs, m.fetchTime, m.bytes, m.inflight,
		m.retries, m.deadLetter, m.leaseRecovered, m.robotsDenied, m.links)
	return m
}

func (m *Metrics) ObserveJob(status string) { m.jobs.WithLabelValues(status).Inc() }
func (m *Metrics) ObserveFetch(kind, outcome string, duration time.Duration) {
	m.fetchTime.WithLabelValues(kind, outcome).Observe(duration.Seconds())
}
func (m *Metrics) AddBytes(kind string, n int64) {
	if n > 0 {
		m.bytes.WithLabelValues(kind).Add(float64(n))
	}
}
func (m *Metrics) IncInflight()       { m.inflight.Inc() }
func (m *Metrics) DecInflight()       { m.inflight.Dec() }
func (m *Metrics) IncRetry()          { m.retries.Inc() }
func (m *Metrics) IncDeadLetter()     { m.deadLetter.Inc() }
func (m *Metrics) IncLeaseRecovered() { m.leaseRecovered.Inc() }
func (m *Metrics) IncLeaseRecoveredN(n int) {
	if n > 0 {
		m.leaseRecovered.Add(float64(n))
	}
}
func (m *Metrics) IncRobotsDenied() { m.robotsDenied.Inc() }
func (m *Metrics) AddLinks(n int)   { m.links.Add(float64(n)) }
