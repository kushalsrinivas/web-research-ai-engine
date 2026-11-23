// Package dispatch owns the crawl frontier: it turns due sources into jobs and
// publishes due jobs to Kafka. Postgres remains the source of truth, so a lost
// Kafka record is redelivered by the stale-dispatch sweep.
package dispatch

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"net/http"
	"time"

	"github.com/go-chi/chi/v5"
	"github.com/twmb/franz-go/pkg/kgo"

	"github.com/kushalsrinivas/web-research-ai-engine/internal/crawlcontract"
	"github.com/kushalsrinivas/web-research-ai-engine/internal/crawler/metrics"
	"github.com/kushalsrinivas/web-research-ai-engine/internal/crawler/store"
	"github.com/kushalsrinivas/web-research-ai-engine/internal/crawler/urlnormalizer"
	"github.com/kushalsrinivas/web-research-ai-engine/internal/sources"
)

type Config struct {
	Interval    time.Duration
	Batch       int
	SourceLimit int
}

func DefaultConfig() Config {
	return Config{Interval: 5 * time.Second, Batch: 200, SourceLimit: 25}
}

type Dispatcher struct {
	store    *store.Store
	sources  *sources.Repository
	producer *kgo.Client
	metrics  *metrics.Metrics
	logger   *slog.Logger
	cfg      Config
}

func New(store *store.Store, sources *sources.Repository, producer *kgo.Client, metrics *metrics.Metrics, logger *slog.Logger, cfg Config) *Dispatcher {
	if cfg.Interval <= 0 {
		cfg = DefaultConfig()
	}
	return &Dispatcher{store: store, sources: sources, producer: producer, metrics: metrics, logger: logger, cfg: cfg}
}

// Run ticks until ctx is cancelled.
func (d *Dispatcher) Run(ctx context.Context) {
	ticker := time.NewTicker(d.cfg.Interval)
	defer ticker.Stop()
	for {
		if err := d.Tick(ctx); err != nil && ctx.Err() == nil {
			d.logger.ErrorContext(ctx, "crawl dispatch failed", "error", err)
		}
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
		}
	}
}

// Tick advances one dispatch cycle. It is safe to run in several schedulers:
// row locks and unique indexes make overlapping work idempotent.
func (d *Dispatcher) Tick(ctx context.Context) error {
	if err := d.enqueueDueSources(ctx); err != nil {
		return fmt.Errorf("enqueue due sources: %w", err)
	}
	if jobs, err := d.store.DueJobs(ctx, d.cfg.Batch); err != nil {
		return fmt.Errorf("load due jobs: %w", err)
	} else {
		d.publish(ctx, jobs)
	}
	if recovered, err := d.store.RecoverLeases(ctx); err != nil {
		return fmt.Errorf("recover leases: %w", err)
	} else if len(recovered) > 0 {
		d.metrics.IncLeaseRecoveredN(len(recovered))
		d.logger.WarnContext(ctx, "recovered expired crawler leases", "count", len(recovered))
	}
	if stale, err := d.store.StaleDispatched(ctx); err != nil {
		return fmt.Errorf("reset stale dispatch: %w", err)
	} else if len(stale) > 0 {
		d.logger.WarnContext(ctx, "reset unpublished crawler jobs", "count", len(stale))
	}
	return nil
}

func (d *Dispatcher) enqueueDueSources(ctx context.Context) error {
	due, err := d.sources.DueSources(ctx, time.Now().UTC(), d.cfg.SourceLimit)
	if err != nil {
		return err
	}
	if len(due) == 0 {
		return nil
	}
	policies, err := d.sources.ListDomainPolicies(ctx)
	if err != nil {
		return err
	}
	byDomain := make(map[string]sources.DomainPolicy, len(policies))
	for _, policy := range policies {
		byDomain[policy.Domain] = policy
	}
	for _, source := range due {
		normalized, err := urlnormalizer.Normalize(source.BaseURL)
		if err != nil {
			d.logger.WarnContext(ctx, "source has invalid base url", "source_id", source.ID, "error", err)
			continue
		}
		policy := crawlcontract.DefaultDomainPolicy()
		if configured, ok := byDomain[source.Domain]; ok {
			policy = crawlcontract.DomainPolicy{
				MaxConcurrency:    configured.MaxConcurrency,
				RequestsPerMinute: configured.RequestsPerMinute,
				CrawlDelayMS:      configured.CrawlDelayMS,
				RobotsPolicy:      configured.RobotsPolicy,
				Allowed:           configured.Allowed,
			}
		}
		job, inserted, err := d.store.Enqueue(ctx, store.EnqueueInput{
			SourceID:           source.ID,
			URL:                normalized.URL,
			URLHash:            normalized.Hash[:],
			Host:               normalized.Host,
			Lane:               "bulk",
			Depth:              0,
			SourceRobotsPolicy: source.RobotsPolicy,
			DomainPolicy:       policy,
			Seed:               true,
		})
		if err != nil {
			d.logger.ErrorContext(ctx, "enqueue source seed failed", "source_id", source.ID, "error", err)
			continue
		}
		if inserted {
			d.publish(ctx, []store.Job{job})
		}
	}
	return nil
}

func (d *Dispatcher) publish(ctx context.Context, jobs []store.Job) {
	for _, job := range jobs {
		message := store.Message(job)
		body, err := json.Marshal(message)
		if err != nil {
			d.logger.ErrorContext(ctx, "marshal crawl job failed", "job_id", job.JobID, "error", err)
			continue
		}
		result := d.producer.ProduceSync(ctx, &kgo.Record{
			Topic: message.Topic(),
			Key:   []byte(job.Host),
			Value: body,
		})
		if err := result.FirstErr(); err != nil {
			d.logger.WarnContext(ctx, "publish crawl job failed", "job_id", job.JobID, "error", err)
			continue
		}
		if err := d.store.MarkDispatched(ctx, job.JobID); err != nil {
			d.logger.WarnContext(ctx, "mark crawl job dispatched failed", "job_id", job.JobID, "error", err)
		}
	}
}

// Mount attaches the operator-facing crawl endpoints.
func (d *Dispatcher) Mount(r chi.Router) {
	r.Route("/internal/v1/crawl", func(r chi.Router) {
		r.Post("/jobs", d.createJob)
		r.Get("/jobs/{id}", d.getJob)
		r.Get("/stats", d.stats)
		r.Post("/dispatch", d.forceDispatch)
	})
}

type createJobRequest struct {
	URL      string `json:"url"`
	SourceID string `json:"source_id"`
	Lane     string `json:"lane"`
	Method   string `json:"method"`
	Depth    int    `json:"depth"`
}

func (d *Dispatcher) createJob(w http.ResponseWriter, r *http.Request) {
	var req createJobRequest
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<20)).Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, "invalid json")
		return
	}
	normalized, err := urlnormalizer.Normalize(req.URL)
	if err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	if req.Lane == "" {
		req.Lane = "interactive"
	}
	if req.Lane != "interactive" && req.Lane != "bulk" {
		writeError(w, http.StatusBadRequest, "lane must be interactive or bulk")
		return
	}
	if req.Method == "" {
		req.Method = http.MethodGet
	}
	if req.Method != http.MethodGet && req.Method != http.MethodHead {
		writeError(w, http.StatusBadRequest, "method must be GET or HEAD")
		return
	}
	policy, err := d.store.DomainPolicy(r.Context(), normalized.Host)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "policy lookup failed")
		return
	}
	robotsPolicy := "respect"
	if req.SourceID != "" {
		source, err := d.sources.GetSource(r.Context(), req.SourceID)
		if err != nil {
			writeError(w, http.StatusBadRequest, "source not found")
			return
		}
		robotsPolicy = source.RobotsPolicy
	}
	job, inserted, err := d.store.Enqueue(r.Context(), store.EnqueueInput{
		SourceID:           req.SourceID,
		URL:                normalized.URL,
		URLHash:            normalized.Hash[:],
		Host:               normalized.Host,
		Lane:               req.Lane,
		Method:             req.Method,
		Depth:              req.Depth,
		SourceRobotsPolicy: robotsPolicy,
		DomainPolicy:       policy,
	})
	if err != nil {
		writeError(w, http.StatusInternalServerError, "enqueue failed")
		return
	}
	if inserted {
		d.publish(r.Context(), []store.Job{job})
	}
	status := http.StatusOK
	if inserted {
		status = http.StatusAccepted
	}
	writeJSON(w, status, map[string]any{"job": job, "inserted": inserted})
}

func (d *Dispatcher) getJob(w http.ResponseWriter, r *http.Request) {
	job, err := d.store.GetJob(r.Context(), chi.URLParam(r, "id"))
	if err != nil {
		writeError(w, http.StatusNotFound, "job not found")
		return
	}
	writeJSON(w, http.StatusOK, job)
}

func (d *Dispatcher) stats(w http.ResponseWriter, r *http.Request) {
	stats, err := d.store.Stats(r.Context())
	if err != nil {
		writeError(w, http.StatusInternalServerError, "stats failed")
		return
	}
	writeJSON(w, http.StatusOK, stats)
}

func (d *Dispatcher) forceDispatch(w http.ResponseWriter, r *http.Request) {
	if err := d.Tick(r.Context()); err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"status": "dispatched"})
}

func writeJSON(w http.ResponseWriter, status int, value any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(value)
}

func writeError(w http.ResponseWriter, status int, message string) {
	writeJSON(w, status, map[string]string{"error": message})
}
