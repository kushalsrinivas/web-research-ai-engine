// Package worker consumes crawl jobs from Kafka and runs the fetch pipeline.
// Ownership is taken in Postgres, so Kafka re-delivery is harmless.
package worker

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"net/url"
	"os"
	"strings"
	"sync"
	"time"

	"github.com/oklog/ulid/v2"
	"github.com/twmb/franz-go/pkg/kgo"
	"go.opentelemetry.io/otel/trace"

	"github.com/kushalsrinivas/web-research-ai-engine/internal/crawlcontract"
	"github.com/kushalsrinivas/web-research-ai-engine/internal/crawler/archive"
	"github.com/kushalsrinivas/web-research-ai-engine/internal/crawler/extract"
	"github.com/kushalsrinivas/web-research-ai-engine/internal/crawler/fetcher"
	"github.com/kushalsrinivas/web-research-ai-engine/internal/crawler/gate"
	"github.com/kushalsrinivas/web-research-ai-engine/internal/crawler/metrics"
	"github.com/kushalsrinivas/web-research-ai-engine/internal/crawler/retry"
	"github.com/kushalsrinivas/web-research-ai-engine/internal/crawler/robots"
	"github.com/kushalsrinivas/web-research-ai-engine/internal/crawler/store"
	"github.com/kushalsrinivas/web-research-ai-engine/internal/crawler/urlnormalizer"
	"github.com/kushalsrinivas/web-research-ai-engine/internal/platform/version"
	"github.com/kushalsrinivas/web-research-ai-engine/internal/sources"
)

type Config struct {
	WorkerID          string
	Concurrency       int
	Lease             time.Duration
	MaxDepth          int
	MaxAttempts       int
	UserAgent         string
	AllowPrivate      bool
	FetchTimeout      time.Duration
	MaxBodyBytes      int64
	HeartbeatInterval time.Duration
	SchedulerURL      string
}

func (c *Config) normalize() {
	if c.Concurrency < 1 {
		c.Concurrency = 1
	}
	if c.Lease <= 0 {
		c.Lease = 10 * time.Minute
	}
	if c.MaxAttempts < 1 {
		c.MaxAttempts = 5
	}
	if c.FetchTimeout <= 0 {
		c.FetchTimeout = 30 * time.Second
	}
	if c.MaxBodyBytes <= 0 {
		c.MaxBodyBytes = 10 << 20
	}
	if c.HeartbeatInterval <= 0 {
		c.HeartbeatInterval = 10 * time.Second
	}
	if c.WorkerID == "" {
		hostname, _ := os.Hostname()
		c.WorkerID = fmt.Sprintf("%s-%s", hostname, ulid.Make().String())
	}
}

type Worker struct {
	cfg      Config
	store    *store.Store
	client   *kgo.Client
	gate     *gate.Gate
	robots   *robots.Service
	fetcher  *fetcher.Fetcher
	archive  archive.Store
	metrics  *metrics.Metrics
	logger   *slog.Logger
	http     *http.Client
	jobLimit time.Duration
}

func New(cfg Config, st *store.Store, client *kgo.Client, robotsSvc *robots.Service,
	f *fetcher.Fetcher, archiveStore archive.Store, m *metrics.Metrics, logger *slog.Logger) *Worker {
	cfg.normalize()
	return &Worker{
		cfg:      cfg,
		store:    st,
		client:   client,
		gate:     gate.New(),
		robots:   robotsSvc,
		fetcher:  f,
		archive:  archiveStore,
		metrics:  m,
		logger:   logger,
		http:     &http.Client{Timeout: 5 * time.Second},
		jobLimit: cfg.FetchTimeout*2 + 30*time.Second,
	}
}

// NewConsumer builds the Kafka client for one worker replica.
func NewConsumer(brokers []string, concurrency int) (*kgo.Client, error) {
	return kgo.NewClient(
		kgo.SeedBrokers(brokers...),
		kgo.ConsumerGroup(crawlcontract.ConsumerGroup),
		kgo.ConsumeTopics(crawlcontract.TopicFetchBulk, crawlcontract.TopicFetchInteractive),
		kgo.ConsumeResetOffset(kgo.NewOffset().AtStart()),
		kgo.BlockRebalanceOnPoll(),
		kgo.DisableAutoCommit(),
		kgo.SessionTimeout(30*time.Second),
		kgo.HeartbeatInterval(3*time.Second),
		kgo.MaxConcurrentFetches(concurrency),
	)
}

// Run consumes until ctx is cancelled.
func (w *Worker) Run(ctx context.Context) error {
	if err := w.store.RegisterWorker(ctx, w.cfg.WorkerID, hostname(), w.cfg.Concurrency); err != nil {
		return fmt.Errorf("register worker: %w", err)
	}
	heartbeatCtx, stopHeartbeat := context.WithCancel(ctx)
	defer stopHeartbeat()
	go w.heartbeat(heartbeatCtx)
	defer func(c context.Context) {
		stopCtx, cancel := context.WithTimeout(context.WithoutCancel(c), 5*time.Second)
		defer cancel()
		_ = w.store.StopWorker(stopCtx, w.cfg.WorkerID)
	}(ctx)

	sem := make(chan struct{}, w.cfg.Concurrency)
	for {
		fetches := w.client.PollFetches(ctx)
		if ctx.Err() != nil {
			return nil
		}
		fetches.EachError(func(topic string, partition int32, err error) {
			if !errors.Is(err, context.Canceled) {
				w.logger.WarnContext(ctx, "crawl consumer error", "topic", topic, "partition", partition, "error", err)
			}
		})
		if fetches.IsClientClosed() {
			return nil
		}

		var wg sync.WaitGroup
		fetches.EachRecord(func(record *kgo.Record) {
			sem <- struct{}{}
			wg.Add(1)
			go func() {
				defer wg.Done()
				defer func() { <-sem }()
				defer func(c context.Context) {
					if recovered := recover(); recovered != nil {
						w.logger.ErrorContext(c, "crawl worker panic", "panic", recovered)
					}
				}(ctx)
				w.handle(ctx, record)
			}()
		})
		wg.Wait()

		if records := fetches.Records(); len(records) > 0 {
			// A failed DB write still leaves the job leased; the lease sweep
			// republishes it after expiry.
			if err := w.client.CommitRecords(ctx, records...); err != nil && ctx.Err() == nil {
				w.logger.WarnContext(ctx, "commit crawl offsets failed", "error", err)
			}
		}
		w.client.AllowRebalance()
	}
}

func (w *Worker) heartbeat(ctx context.Context) {
	ticker := time.NewTicker(w.cfg.HeartbeatInterval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			if err := w.store.HeartbeatWorker(ctx, w.cfg.WorkerID); err != nil {
				w.logger.WarnContext(ctx, "worker heartbeat failed", "error", err)
			}
		}
	}
}

func (w *Worker) handle(ctx context.Context, record *kgo.Record) {
	var message crawlcontract.JobMessage
	if err := json.Unmarshal(record.Value, &message); err != nil {
		w.logger.WarnContext(ctx, "drop malformed crawl message", "error", err)
		return
	}
	if message.JobID == "" {
		return
	}
	job, claimed, err := w.store.Claim(ctx, message.JobID, w.cfg.WorkerID, w.cfg.Lease)
	if err != nil {
		w.logger.ErrorContext(ctx, "claim crawl job failed", "job_id", message.JobID, "error", err)
		return
	}
	if !claimed {
		return
	}

	jobCtx, cancel := context.WithTimeout(ctx, w.jobLimit)
	defer cancel()
	w.metrics.IncInflight()
	defer w.metrics.DecInflight()
	started := time.Now()
	traceID := traceIDFrom(jobCtx)

	sourcePolicy := message.SourceRobotsPolicy
	if sourcePolicy == "" {
		sourcePolicy = "respect"
	}
	if !message.DomainPolicy.Allowed || sourcePolicy == "deny" ||
		message.DomainPolicy.RobotsPolicy == "deny" {
		w.skip(jobCtx, job, "domain policy denies crawling", traceID, started)
		return
	}

	var robotsDelay time.Duration
	if sourcePolicy == "respect" && message.DomainPolicy.RobotsPolicy != "allow" {
		rules, err := w.robots.Rules(jobCtx, job.URL)
		if err != nil {
			w.fail(jobCtx, job, store.Failure{
				Error: fmt.Sprintf("robots lookup: %v", err), Retryable: true,
				TraceID: traceID, DurationMS: millis(started),
			})
			return
		}
		parsed, _ := parsePath(job.URL)
		if !rules.Allowed(parsed) {
			w.metrics.IncRobotsDenied()
			w.skip(jobCtx, job, "robots.txt disallows "+parsed, traceID, started)
			return
		}
		robotsDelay = rules.CrawlDelay
	}

	release, err := w.gate.Acquire(jobCtx, job.Host, message.DomainPolicy, robotsDelay)
	if err != nil {
		w.fail(jobCtx, job, store.Failure{
			Error: "cancelled while waiting for domain slot", Retryable: true,
			TraceID: traceID, DurationMS: millis(started),
		})
		return
	}
	defer release()

	cached, err := w.store.CachedResponse(jobCtx, job.URLHash)
	if err != nil {
		w.logger.WarnContext(jobCtx, "read http cache failed", "job_id", job.JobID, "error", err)
	}
	var entry *fetcher.CacheEntry
	if cached != nil {
		entry = &fetcher.CacheEntry{ETag: cached.ETag, LastModified: cached.LastModified}
	}

	result, err := w.fetcher.Fetch(jobCtx, job.URL, job.Method, entry)
	if err != nil {
		w.fail(jobCtx, job, store.Failure{
			Error: err.Error(), Retryable: true, TraceID: traceID, DurationMS: millis(started),
		})
		w.reportOutcome(jobCtx, message, sources.CrawlOutcome{Status: "failure", Error: err.Error()})
		return
	}
	w.metrics.ObserveFetch(string(result.ContentKind), outcomeFor(result.HTTPStatus), time.Since(started))

	if result.HTTPStatus == http.StatusNotModified || job.Method == http.MethodHead {
		if err := w.store.Complete(jobCtx, job, w.cfg.WorkerID, store.FetchResult{
			HTTPStatus: result.HTTPStatus, ContentType: result.ContentType,
			NotModified: true, TraceID: traceID, DurationMS: millis(started),
			ProcessorVersion: version.String("fetcher"),
		}); err != nil {
			w.logger.ErrorContext(jobCtx, "complete not-modified job failed", "job_id", job.JobID, "error", err)
		}
		w.metrics.ObserveJob("archived")
		w.reportOutcome(jobCtx, message, sources.CrawlOutcome{Status: "success", HTTPStatus: &result.HTTPStatus})
		return
	}

	if retryableStatus(result.HTTPStatus) {
		retryAfter := int(result.RetryAfter.Seconds())
		w.fail(jobCtx, job, store.Failure{
			Error:      fmt.Sprintf("http status %d", result.HTTPStatus),
			HTTPStatus: &result.HTTPStatus,
			Retryable:  true,
			RetryAfter: result.RetryAfter,
			TraceID:    traceID, DurationMS: millis(started),
		})
		w.reportOutcome(jobCtx, message, sources.CrawlOutcome{
			Status: "failure", HTTPStatus: &result.HTTPStatus,
			Error:             fmt.Sprintf("http status %d", result.HTTPStatus),
			RetryAfterSeconds: &retryAfter,
		})
		return
	}
	if result.HTTPStatus >= 400 {
		w.fail(jobCtx, job, store.Failure{
			Error:      fmt.Sprintf("http status %d", result.HTTPStatus),
			HTTPStatus: &result.HTTPStatus, Retryable: false,
			TraceID: traceID, DurationMS: millis(started),
		})
		w.reportOutcome(jobCtx, message, sources.CrawlOutcome{
			Status: "failure", HTTPStatus: &result.HTTPStatus,
			Error: fmt.Sprintf("http status %d", result.HTTPStatus),
		})
		return
	}

	archived, err := w.archive.Archive(jobCtx, archive.Record{
		TenantID:       job.TenantID,
		SourceID:       sourceID(job),
		DocumentID:     job.DocumentID,
		RequestedURL:   job.URL,
		FinalURL:       result.FinalURL,
		HTTPStatus:     result.HTTPStatus,
		ContentType:    result.ContentType,
		ContentKind:    result.ContentKind,
		FetchedAt:      result.FetchedAt,
		Body:           result.Body,
		ResponseHeader: result.Header,
	})
	if err != nil {
		w.fail(jobCtx, job, store.Failure{
			Error: "archive: " + err.Error(), HTTPStatus: &result.HTTPStatus,
			Retryable: true, TraceID: traceID, DurationMS: millis(started),
		})
		w.reportOutcome(jobCtx, message, sources.CrawlOutcome{Status: "failure", HTTPStatus: &result.HTTPStatus,
			Error: "archive failed"})
		return
	}
	w.metrics.AddBytes(string(result.ContentKind), archived.Bytes)

	if err := w.store.Complete(jobCtx, job, w.cfg.WorkerID, store.FetchResult{
		HTTPStatus:       result.HTTPStatus,
		ContentType:      result.ContentType,
		ContentKind:      string(result.ContentKind),
		ContentHash:      archived.Digest,
		ArtifactURI:      archived.URI,
		Bytes:            archived.Bytes,
		FinalURL:         result.FinalURL,
		DurationMS:       millis(started),
		TraceID:          traceID,
		ProcessorVersion: version.String("fetcher"),
	}); err != nil {
		w.logger.ErrorContext(jobCtx, "complete crawl job failed", "job_id", job.JobID, "error", err)
		return
	}
	w.metrics.ObserveJob("archived")
	if err := w.store.SaveCachedResponse(jobCtx, job.URLHash, job.URL,
		result.ETag, result.LastModified, archived.Digest, result.HTTPStatus); err != nil {
		w.logger.WarnContext(jobCtx, "save http cache failed", "job_id", job.JobID, "error", err)
	}

	links := w.discover(jobCtx, job, result, message)
	w.reportOutcome(jobCtx, message, sources.CrawlOutcome{
		Status: "success", HTTPStatus: &result.HTTPStatus, ItemsFound: links,
	})
}

func (w *Worker) discover(ctx context.Context, job store.Job, result fetcher.Result, message crawlcontract.JobMessage) int {
	if job.Depth >= w.cfg.MaxDepth || !result.ContentKind.LinksAllowed() {
		return 0
	}
	base, err := parseURL(result.FinalURL)
	if err != nil {
		return 0
	}
	links := extract.Links(result.Body, base, 0)
	inserted := 0
	policies := map[string]crawlcontract.DomainPolicy{}
	for _, link := range links {
		normalized, err := urlnormalizer.Normalize(link)
		if err != nil || !strings.EqualFold(normalized.Host, job.Host) {
			continue
		}
		policy, ok := policies[normalized.Host]
		if !ok {
			policy, err = w.store.DomainPolicy(ctx, normalized.Host)
			if err != nil {
				continue
			}
			policies[normalized.Host] = policy
		}
		_, wasInserted, err := w.store.Enqueue(ctx, store.EnqueueInput{
			SourceID:           sourceID(job),
			URL:                normalized.URL,
			URLHash:            normalized.Hash[:],
			Host:               normalized.Host,
			TenantID:           job.TenantID,
			Lane:               job.Lane,
			Depth:              job.Depth + 1,
			SourceRobotsPolicy: message.SourceRobotsPolicy,
			DomainPolicy:       policy,
		})
		if err != nil {
			w.logger.WarnContext(ctx, "enqueue discovered link failed", "url", normalized.URL, "error", err)
			continue
		}
		if wasInserted {
			inserted++
		}
	}
	w.metrics.AddLinks(inserted)
	return inserted
}

func (w *Worker) fail(ctx context.Context, job store.Job, failure store.Failure) {
	if failure.ProcessorVersion == "" {
		failure.ProcessorVersion = version.String("fetcher")
	}
	status, err := w.store.Fail(ctx, job, w.cfg.WorkerID, failure, retry.Backoff(job.Attempts, failure.RetryAfter))
	if err != nil {
		w.logger.ErrorContext(ctx, "record crawl failure failed", "job_id", job.JobID, "error", err)
		return
	}
	w.metrics.ObserveJob(status)
	if status == "retrying" {
		w.metrics.IncRetry()
	}
	if status == "dead_lettered" {
		w.metrics.IncDeadLetter()
	}
}

func (w *Worker) skip(ctx context.Context, job store.Job, reason, traceID string, started time.Time) {
	if err := w.store.Skip(ctx, job, w.cfg.WorkerID, reason); err != nil {
		w.logger.ErrorContext(ctx, "skip crawl job failed", "job_id", job.JobID, "error", err)
		return
	}
	w.metrics.ObserveJob("skipped")
	w.logger.InfoContext(ctx, "crawl skipped by policy", "job_id", job.JobID, "reason", reason)
	_ = traceID
	_ = started
	if job.SourceID != nil && job.Depth == 0 {
		w.reportOutcome(ctx, crawlcontract.JobMessage{SourceID: *job.SourceID}, sources.CrawlOutcome{
			Status: "skipped", Error: reason,
		})
	}
}

func (w *Worker) reportOutcome(parent context.Context, message crawlcontract.JobMessage, outcome sources.CrawlOutcome) {
	if message.SourceID == "" || message.Depth != 0 {
		return
	}
	body, err := json.Marshal(outcome)
	if err != nil {
		return
	}
	ctx, cancel := context.WithTimeout(context.WithoutCancel(parent), 5*time.Second)
	defer cancel()
	url := strings.TrimRight(w.cfg.SchedulerURL, "/") + "/internal/v1/sources/" + message.SourceID + "/outcomes"
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, strings.NewReader(string(body)))
	if err != nil {
		return
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := w.http.Do(req)
	if err != nil {
		w.logger.Warn("report crawl outcome failed", "source_id", message.SourceID, "error", err)
		return
	}
	_ = resp.Body.Close()
	if resp.StatusCode >= 300 {
		w.logger.Warn("report crawl outcome rejected", "source_id", message.SourceID, "status", resp.StatusCode)
	}
}

func outcomeFor(status int) string {
	switch {
	case status >= 200 && status < 300:
		return "success"
	case status == http.StatusNotModified:
		return "not_modified"
	case status >= 400 && status < 500:
		return "client_error"
	default:
		return "server_error"
	}
}

func retryableStatus(status int) bool {
	return status == http.StatusTooManyRequests || status == http.StatusRequestTimeout || status >= 500
}

func traceIDFrom(ctx context.Context) string {
	span := trace.SpanContextFromContext(ctx)
	if !span.IsValid() {
		return ""
	}
	return span.TraceID().String()
}

func millis(start time.Time) int { return int(time.Since(start).Milliseconds()) }

func parsePath(rawURL string) (string, error) {
	parsed, err := url.Parse(rawURL)
	if err != nil {
		return "/", err
	}
	if parsed.Path == "" {
		return "/", nil
	}
	return parsed.Path, nil
}

func parseURL(rawURL string) (*url.URL, error) { return url.Parse(rawURL) }

func hostname() string {
	name, err := os.Hostname()
	if err != nil {
		return "unknown"
	}
	return name
}

func sourceID(job store.Job) string {
	if job.SourceID == nil {
		return ""
	}
	return *job.SourceID
}
