// Package store persists crawl jobs, worker leases, and document identity.
// Postgres is the source of truth; Kafka only wakes consumers.
package store

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/oklog/ulid/v2"

	"github.com/kushalsrinivas/web-research-ai-engine/internal/crawlcontract"
)

const jobColumns = `job_id, source_id, document_id, url, url_hash, host, tenant_id, lane,
	method, depth, max_attempts, attempts, status, http_status,
	coalesce(content_type, ''), coalesce(artifact_uri, ''),
	coalesce(last_error, ''), payload`

type Job struct {
	JobID       string         `json:"job_id"`
	SourceID    *string        `json:"source_id,omitempty"`
	DocumentID  string         `json:"document_id"`
	URL         string         `json:"url"`
	URLHash     []byte         `json:"-"`
	Host        string         `json:"host"`
	TenantID    string         `json:"tenant_id"`
	Lane        string         `json:"lane"`
	Method      string         `json:"method"`
	Depth       int            `json:"depth"`
	Status      string         `json:"status"`
	MaxAttempts int            `json:"max_attempts"`
	Attempts    int            `json:"attempts"`
	HTTPStatus  *int           `json:"http_status,omitempty"`
	ContentType string         `json:"content_type,omitempty"`
	ArtifactURI string         `json:"artifact_uri,omitempty"`
	LastError   string         `json:"last_error,omitempty"`
	Payload     map[string]any `json:"payload,omitempty"`
}

type EnqueueInput struct {
	SourceID           string
	URL                string
	URLHash            []byte
	Host               string
	TenantID           string
	Lane               string
	Method             string
	Depth              int
	SourceRobotsPolicy string
	DomainPolicy       crawlcontract.DomainPolicy
	Seed               bool
}

type FetchResult struct {
	HTTPStatus       int
	ContentType      string
	ContentKind      string
	ContentHash      []byte
	ArtifactURI      string
	Bytes            int64
	FinalURL         string
	NotModified      bool
	DurationMS       int
	TraceID          string
	ProcessorVersion string
}

type Failure struct {
	Error            string
	HTTPStatus       *int
	Retryable        bool
	RetryAfter       time.Duration
	DurationMS       int
	TraceID          string
	ProcessorVersion string
}

type CachedResponse struct {
	ETag         string
	LastModified time.Time
	ContentHash  []byte
	LastStatus   int
}

type Stats struct {
	JobsByStatus  map[string]int64 `json:"jobs_by_status"`
	WorkersActive int64            `json:"workers_active"`
	JobsTotal     int64            `json:"jobs_total"`
}

type Store struct {
	pool *pgxpool.Pool
}

func New(pool *pgxpool.Pool) *Store {
	return &Store{pool: pool}
}

// Enqueue records document identity and inserts a job. It returns inserted
// false when an equivalent active job already exists.
func (s *Store) Enqueue(ctx context.Context, input EnqueueInput) (Job, bool, error) {
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return Job{}, false, err
	}
	defer func() { _ = tx.Rollback(ctx) }()

	if input.Seed && input.SourceID != "" {
		var recent bool
		err := tx.QueryRow(ctx, `select exists (
			select 1 from fetcher.jobs
			where source_id = $1::uuid and depth = 0
			  and created_at > now() - interval '60 seconds')`, input.SourceID).Scan(&recent)
		if err != nil {
			return Job{}, false, err
		}
		if recent {
			return Job{}, false, nil
		}
	}

	if input.TenantID == "" {
		input.TenantID = "public"
	}
	if input.Lane == "" {
		input.Lane = "bulk"
	}
	if input.Method == "" {
		input.Method = "GET"
	}
	if input.SourceRobotsPolicy == "" {
		input.SourceRobotsPolicy = "respect"
	}
	documentID := ulid.Make().String()
	err = tx.QueryRow(ctx, `
		insert into core.documents (document_id, tenant_id, canonical_url, url_hash, host)
		values ($1, $2, $3, $4, $5)
		on conflict (tenant_id, url_hash) do update set last_seen_at = now()
		returning document_id`,
		documentID, input.TenantID, input.URL, input.URLHash, input.Host).Scan(&documentID)
	if err != nil {
		return Job{}, false, err
	}

	payload, err := json.Marshal(map[string]any{
		"domain_policy":        input.DomainPolicy,
		"source_robots_policy": input.SourceRobotsPolicy,
	})
	if err != nil {
		return Job{}, false, err
	}
	var sourceID any
	if input.SourceID != "" {
		sourceID = input.SourceID
	}
	jobID := ulid.Make().String()
	var job Job
	err = scanJob(tx.QueryRow(ctx, `
		insert into fetcher.jobs (job_id, source_id, document_id, url, url_hash, host,
			tenant_id, lane, method, depth, payload)
		values ($1, $2::uuid, $3, $4, $5, $6, $7, $8, $9, $10, $11)
		on conflict do nothing
		returning `+jobColumns,
		jobID, sourceID, documentID, input.URL, input.URLHash, input.Host,
		input.TenantID, input.Lane, input.Method, input.Depth, payload), &job)
	if errors.Is(err, pgx.ErrNoRows) {
		return Job{}, false, nil
	}
	if err != nil {
		return Job{}, false, err
	}
	if err := tx.Commit(ctx); err != nil {
		return Job{}, false, err
	}
	return job, true, nil
}

// Claim takes a lease only if no other worker owns the job. Re-delivered Kafka
// messages therefore cannot cause duplicate side effects.
func (s *Store) Claim(ctx context.Context, jobID, owner string, lease time.Duration) (Job, bool, error) {
	var job Job
	err := scanJob(s.pool.QueryRow(ctx, `
		update fetcher.jobs
		set status = 'leased', lease_owner = $2,
		    lease_expires_at = now() + ($3 * interval '1 second'),
		    attempts = attempts + 1, updated_at = now()
		where job_id = $1 and status in ('pending', 'dispatched', 'retrying')
		  and next_attempt_at <= now()
		returning `+jobColumns, jobID, owner, lease.Seconds()), &job)
	if errors.Is(err, pgx.ErrNoRows) {
		return Job{}, false, nil
	}
	if err != nil {
		return Job{}, false, err
	}
	return job, true, nil
}

// Complete marks the job archived and appends the document revision.
func (s *Store) Complete(ctx context.Context, job Job, owner string, result FetchResult) error {
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return err
	}
	defer func() { _ = tx.Rollback(ctx) }()

	tag, err := tx.Exec(ctx, `
		update fetcher.jobs
		set status = 'archived', http_status = $3, content_type = $4,
		    content_hash = $5, artifact_uri = $6, last_error = null,
		    lease_owner = null, lease_expires_at = null, updated_at = now()
		where job_id = $1 and lease_owner = $2`,
		job.JobID, owner, result.HTTPStatus, result.ContentType,
		result.ContentHash, result.ArtifactURI)
	if err != nil {
		return err
	}
	if tag.RowsAffected() == 0 {
		return fmt.Errorf("job %s is no longer leased by %s", job.JobID, owner)
	}
	if result.NotModified {
		if _, err := tx.Exec(ctx, `update core.documents
			set last_fetched_at = now(), last_seen_at = now(), updated_at = now()
			where document_id = $1`, job.DocumentID); err != nil {
			return err
		}
		return tx.Commit(ctx)
	}

	// Recrawls that return identical bytes do not create a revision.
	var previous []byte
	err = tx.QueryRow(ctx, `select content_hash_raw from core.document_revisions
		where document_id = $1 order by revision desc limit 1`, job.DocumentID).Scan(&previous)
	if err == nil && len(previous) > 0 && bytes.Equal(previous, result.ContentHash) {
		if _, err := tx.Exec(ctx, `update core.documents
			set last_fetched_at = now(), last_seen_at = now(), updated_at = now()
			where document_id = $1`, job.DocumentID); err != nil {
			return err
		}
		return tx.Commit(ctx)
	}
	if err != nil && !errors.Is(err, pgx.ErrNoRows) {
		return err
	}

	var revision int
	if err := tx.QueryRow(ctx, `
		update core.documents
		set current_revision = case
		      when exists (select 1 from core.document_revisions where document_id = $1)
		        then current_revision + 1
		      else current_revision
		    end,
		    state = 'raw_archived',
		    last_fetched_at = now(), last_seen_at = now(), updated_at = now()
		where document_id = $1
		returning current_revision`, job.DocumentID).Scan(&revision); err != nil {
		return err
	}
	if _, err := tx.Exec(ctx, `
		insert into core.document_revisions (document_id, revision, fetched_url, http_status,
			content_type, content_hash_raw, raw_artifact_uri, artifact_bytes, renderer_version)
		values ($1, $2, $3, $4, $5, $6, $7, $8, 'http')`,
		job.DocumentID, revision, result.FinalURL, result.HTTPStatus, result.ContentType,
		result.ContentHash, result.ArtifactURI, result.Bytes); err != nil {
		return err
	}
	if err := insertEvent(ctx, tx, job.DocumentID, revision, "crawl.fetch", "raw_archived",
		job.Attempts, result.TraceID, result.ProcessorVersion, result.ArtifactURI, result.DurationMS); err != nil {
		return err
	}
	return tx.Commit(ctx)
}

// Fail records a terminal or retryable failure and returns the job status.
func (s *Store) Fail(ctx context.Context, job Job, owner string, failure Failure, backoff time.Duration) (string, error) {
	status := "retrying"
	nextAttempt := time.Now().UTC().Add(backoff)
	if !failure.Retryable || job.Attempts >= job.MaxAttempts {
		nextAttempt = time.Now().UTC()
		if failure.Retryable {
			status = "dead_lettered"
		} else {
			status = "failed"
		}
	}

	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return "", err
	}
	defer func() { _ = tx.Rollback(ctx) }()

	if _, err := tx.Exec(ctx, `
		update fetcher.jobs
		set status = $3, http_status = $4, last_error = $5, next_attempt_at = $6,
		    lease_owner = null, lease_expires_at = null, updated_at = now()
		where job_id = $1 and lease_owner = $2`,
		job.JobID, owner, status, failure.HTTPStatus, failure.Error, nextAttempt); err != nil {
		return "", err
	}

	docState := "failed"
	if status == "dead_lettered" {
		docState = "dead_lettered"
	}
	if status != "retrying" {
		var revision int
		if err := tx.QueryRow(ctx, `select current_revision from core.documents where document_id = $1`,
			job.DocumentID).Scan(&revision); err != nil {
			return "", err
		}
		if _, err := tx.Exec(ctx, `
			update core.documents
			set state = $2, attempt = $3, failed_stage = 'crawl.fetch',
			    last_error = jsonb_build_object('error', $4::text, 'http_status', $5::int),
			    updated_at = now()
			where document_id = $1 and state not in (
				'raw_archived', 'parsed', 'normalized', 'deduplicated',
				'enriched', 'indexed', 'kg_updated', 'available')`,
			job.DocumentID, docState, job.Attempts, failure.Error, failure.HTTPStatus); err != nil {
			return "", err
		}
		if err := insertEvent(ctx, tx, job.DocumentID, revision, "crawl.fetch", docState,
			job.Attempts, failure.TraceID, failure.ProcessorVersion, "", failure.DurationMS); err != nil {
			return "", err
		}
	}
	if err := tx.Commit(ctx); err != nil {
		return "", err
	}
	return status, nil
}

// Skip records a policy block without retrying.
func (s *Store) Skip(ctx context.Context, job Job, owner, reason string) error {
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return err
	}
	defer func() { _ = tx.Rollback(ctx) }()
	if _, err := tx.Exec(ctx, `
		update fetcher.jobs
		set status = 'skipped', last_error = $3, lease_owner = null,
		    lease_expires_at = null, updated_at = now()
		where job_id = $1 and lease_owner = $2`, job.JobID, owner, reason); err != nil {
		return err
	}
	var revision int
	if err := tx.QueryRow(ctx, `select current_revision from core.documents where document_id = $1`,
		job.DocumentID).Scan(&revision); err != nil {
		return err
	}
	if _, err := tx.Exec(ctx, `
		update core.documents
		set state = 'quarantined', failed_stage = 'crawl.policy',
		    last_error = jsonb_build_object('error', $2::text), updated_at = now()
		where document_id = $1 and state not in (
			'raw_archived', 'parsed', 'normalized', 'deduplicated',
			'enriched', 'indexed', 'kg_updated', 'available')`, job.DocumentID, reason); err != nil {
		return err
	}
	if err := insertEvent(ctx, tx, job.DocumentID, revision, "crawl.policy", "quarantined",
		job.Attempts, job.JobID, "fetcher", "", 0); err != nil {
		return err
	}
	return tx.Commit(ctx)
}

func (s *Store) GetJob(ctx context.Context, jobID string) (Job, error) {
	var job Job
	err := scanJob(s.pool.QueryRow(ctx, `select `+jobColumns+` from fetcher.jobs where job_id = $1`, jobID), &job)
	return job, err
}

// MarkDispatched records that a job was handed to Kafka.
func (s *Store) MarkDispatched(ctx context.Context, jobID string) error {
	_, err := s.pool.Exec(ctx, `
		update fetcher.jobs set status = 'dispatched', updated_at = now()
		where job_id = $1 and status = 'pending'`, jobID)
	return err
}

// DueJobs returns jobs that need to be published, plus recovered leases.
func (s *Store) DueJobs(ctx context.Context, limit int) ([]Job, error) {
	rows, err := s.pool.Query(ctx, `
		update fetcher.jobs
		set status = 'dispatched', updated_at = now()
		where job_id in (
			select job_id from fetcher.jobs
			where status in ('pending', 'retrying') and next_attempt_at <= now()
			order by next_attempt_at
			limit $1
			for update skip locked
		)
		returning `+jobColumns, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	return collectJobs(rows)
}

// RecoverLeases returns jobs whose worker stopped heartbeating.
func (s *Store) RecoverLeases(ctx context.Context) ([]Job, error) {
	rows, err := s.pool.Query(ctx, `
		update fetcher.jobs
		set status = 'retrying', lease_owner = null, lease_expires_at = null,
		    next_attempt_at = now(), last_error = coalesce(last_error, 'lease expired'),
		    updated_at = now()
		where status = 'leased' and lease_expires_at < now()
		returning `+jobColumns)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	return collectJobs(rows)
}

// StaleDispatched resets publish attempts whose Kafka record was lost.
func (s *Store) StaleDispatched(ctx context.Context) ([]Job, error) {
	rows, err := s.pool.Query(ctx, `
		update fetcher.jobs
		set status = 'retrying', next_attempt_at = now(), updated_at = now()
		where status = 'dispatched' and updated_at < now() - interval '2 minutes'
		returning `+jobColumns)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	return collectJobs(rows)
}

func (s *Store) CachedResponse(ctx context.Context, urlHash []byte) (*CachedResponse, error) {
	var cached CachedResponse
	var lastModified *time.Time
	err := s.pool.QueryRow(ctx, `
		select coalesce(etag, ''), last_modified, content_hash, coalesce(last_status, 0)
		from fetcher.http_cache where url_hash = $1`, urlHash).
		Scan(&cached.ETag, &lastModified, &cached.ContentHash, &cached.LastStatus)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	if lastModified != nil {
		cached.LastModified = *lastModified
	}
	return &cached, nil
}

func (s *Store) SaveCachedResponse(ctx context.Context, urlHash []byte, url, etag string, lastModified time.Time, contentHash []byte, status int) error {
	var modified any
	if !lastModified.IsZero() {
		modified = lastModified
	}
	_, err := s.pool.Exec(ctx, `
		insert into fetcher.http_cache (url_hash, url, etag, last_modified, content_hash, last_status, last_fetched_at)
		values ($1, $2, nullif($3, ''), $4, $5, $6, now())
		on conflict (url_hash) do update set
			url = excluded.url, etag = excluded.etag, last_modified = excluded.last_modified,
			content_hash = excluded.content_hash, last_status = excluded.last_status,
			last_fetched_at = excluded.last_fetched_at`,
		urlHash, url, etag, modified, contentHash, status)
	return err
}

// DomainPolicy returns the configured policy for a host, or the default.
func (s *Store) DomainPolicy(ctx context.Context, host string) (crawlcontract.DomainPolicy, error) {
	var policy crawlcontract.DomainPolicy
	err := s.pool.QueryRow(ctx, `
		select max_concurrency, requests_per_minute, crawl_delay_ms, robots_policy, allowed
		from ingest.source_domain_policies where domain = $1`, host).
		Scan(&policy.MaxConcurrency, &policy.RequestsPerMinute, &policy.CrawlDelayMS,
			&policy.RobotsPolicy, &policy.Allowed)
	if errors.Is(err, pgx.ErrNoRows) {
		return crawlcontract.DefaultDomainPolicy(), nil
	}
	return policy, err
}

func (s *Store) RegisterWorker(ctx context.Context, workerID, hostname string, concurrency int) error {
	_, err := s.pool.Exec(ctx, `
		insert into fetcher.workers (worker_id, hostname, concurrency, status)
		values ($1, $2, $3, 'active')
		on conflict (worker_id) do update set
			hostname = excluded.hostname, concurrency = excluded.concurrency,
			status = 'active', heartbeat_at = now()`,
		workerID, hostname, concurrency)
	return err
}

func (s *Store) HeartbeatWorker(ctx context.Context, workerID string) error {
	_, err := s.pool.Exec(ctx, `
		update fetcher.workers set heartbeat_at = now()
		where worker_id = $1`, workerID)
	return err
}

func (s *Store) StopWorker(ctx context.Context, workerID string) error {
	_, err := s.pool.Exec(ctx, `
		update fetcher.workers set status = 'stopping', heartbeat_at = now()
		where worker_id = $1`, workerID)
	return err
}

func (s *Store) Stats(ctx context.Context) (Stats, error) {
	stats := Stats{JobsByStatus: map[string]int64{}}
	rows, err := s.pool.Query(ctx, `
		select status, count(*) from fetcher.jobs group by status`)
	if err != nil {
		return Stats{}, err
	}
	for rows.Next() {
		var status string
		var count int64
		if err := rows.Scan(&status, &count); err != nil {
			rows.Close()
			return Stats{}, err
		}
		stats.JobsByStatus[status] = count
		stats.JobsTotal += count
	}
	rows.Close()
	if err := rows.Err(); err != nil {
		return Stats{}, err
	}
	if err := s.pool.QueryRow(ctx, `
		select count(*) from fetcher.workers
		where status = 'active' and heartbeat_at > now() - interval '1 minute'`).
		Scan(&stats.WorkersActive); err != nil {
		return Stats{}, err
	}
	return stats, nil
}

func (s *Store) Pool() *pgxpool.Pool { return s.pool }

func insertEvent(ctx context.Context, tx pgx.Tx, documentID string, revision int, stage, toState string, attempt int, traceID, processorVersion, artifactURI string, durationMS int) error {
	if traceID == "" {
		traceID = documentID
	}
	if processorVersion == "" {
		processorVersion = "dev"
	}
	_, err := tx.Exec(ctx, `
		insert into core.document_events (document_id, revision, stage, from_state, to_state,
			attempt, processor, processor_version, trace_id, artifact_uri, raw_artifact_uri, duration_ms)
		values ($1, $2, $3, null, $4, $5, 'fetcher', $6, $7, nullif($8, ''), nullif($8, ''), $9)`,
		documentID, revision, stage, toState, attempt, processorVersion, traceID, artifactURI, durationMS)
	return err
}

func scanJob(row pgx.Row, job *Job) error {
	var payload []byte
	if err := row.Scan(&job.JobID, &job.SourceID, &job.DocumentID, &job.URL, &job.URLHash,
		&job.Host, &job.TenantID, &job.Lane, &job.Method, &job.Depth, &job.MaxAttempts,
		&job.Attempts, &job.Status, &job.HTTPStatus, &job.ContentType, &job.ArtifactURI,
		&job.LastError, &payload); err != nil {
		return err
	}
	job.Payload = map[string]any{}
	_ = json.Unmarshal(payload, &job.Payload)
	return nil
}

func collectJobs(rows pgx.Rows) ([]Job, error) {
	jobs := make([]Job, 0)
	for rows.Next() {
		var job Job
		if err := scanJob(rows, &job); err != nil {
			return nil, err
		}
		jobs = append(jobs, job)
	}
	return jobs, rows.Err()
}

// Message converts a job into its Kafka wire form.
func Message(job Job) crawlcontract.JobMessage {
	msg := crawlcontract.JobMessage{
		JobID:      job.JobID,
		DocumentID: job.DocumentID,
		URL:        job.URL,
		TenantID:   job.TenantID,
		Lane:       job.Lane,
		Method:     job.Method,
		Depth:      job.Depth,
		EnqueuedAt: time.Now().UTC(),
	}
	if job.SourceID != nil {
		msg.SourceID = *job.SourceID
	}
	if raw, ok := job.Payload["domain_policy"]; ok {
		if data, err := json.Marshal(raw); err == nil {
			_ = json.Unmarshal(data, &msg.DomainPolicy)
		}
	}
	if msg.DomainPolicy.MaxConcurrency < 1 {
		msg.DomainPolicy = crawlcontract.DefaultDomainPolicy()
	}
	if raw, ok := job.Payload["source_robots_policy"].(string); ok {
		msg.SourceRobotsPolicy = raw
	}
	if msg.SourceRobotsPolicy == "" {
		msg.SourceRobotsPolicy = "respect"
	}
	return msg
}
