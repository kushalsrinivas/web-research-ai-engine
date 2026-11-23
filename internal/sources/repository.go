package sources

import (
	"context"
	"encoding/json"
	"errors"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgxpool"
)

var (
	ErrNotFound = errors.New("source not found")
	ErrConflict = errors.New("source already exists")
)

const sourceColumns = `id::text, name, base_url, source_type, domain, coalesce(language, ''),
	crawl_frequency_seconds, priority, robots_policy, crawl_enabled, status,
	last_crawled_at, next_crawl_at, reliability_score::float8, metadata, created_at, updated_at`

const policyColumns = `domain, max_concurrency, requests_per_minute, crawl_delay_ms,
	robots_policy, allowed, coalesce(notes, ''), metadata, created_at, updated_at`

const healthColumns = `source_id::text, state, consecutive_failures, total_successes, total_failures,
	last_success_at, last_failure_at, coalesce(last_error, ''), last_retry_after_seconds, updated_at`

// Repository is the source registry persistence layer.
type Repository struct {
	pool *pgxpool.Pool
}

func NewRepository(pool *pgxpool.Pool) *Repository {
	return &Repository{pool: pool}
}

// CreateSource inserts a source, its health projection, and configuration v1.
func (r *Repository) CreateSource(ctx context.Context, source Source, createdBy string) (Source, error) {
	if err := ValidateSource(&source); err != nil {
		return Source{}, err
	}
	now := time.Now().UTC()
	next := InitialNextCrawl(source, now)
	metadata, _ := json.Marshal(source.Metadata)

	tx, err := r.pool.Begin(ctx)
	if err != nil {
		return Source{}, err
	}
	defer func() { _ = tx.Rollback(ctx) }()

	row := tx.QueryRow(ctx, `
		insert into ingest.sources
			(name, base_url, source_type, domain, language, crawl_frequency_seconds,
			 priority, robots_policy, crawl_enabled, status, next_crawl_at, metadata)
		values ($1, $2, $3, $4, nullif($5, ''), $6, $7, $8, $9, $10, $11, $12::jsonb)
		returning `+sourceColumns,
		source.Name, source.BaseURL, source.SourceType, source.Domain, source.Language,
		source.CrawlFrequencySeconds, source.Priority, source.RobotsPolicy,
		source.CrawlEnabled, source.Status, next, string(metadata),
	)
	created, err := scanSource(row)
	if err != nil {
		return Source{}, mapPostgresError(err)
	}
	if _, err := tx.Exec(ctx, `insert into ingest.source_health (source_id) values ($1::uuid)`, created.ID); err != nil {
		return Source{}, err
	}
	if err := insertConfigVersion(ctx, tx, created, createdBy); err != nil {
		return Source{}, err
	}
	if err := tx.Commit(ctx); err != nil {
		return Source{}, mapPostgresError(err)
	}
	return created, nil
}

// GetSource returns one registry entry.
func (r *Repository) GetSource(ctx context.Context, id string) (Source, error) {
	row := r.pool.QueryRow(ctx, `select `+sourceColumns+` from ingest.sources where id = $1::uuid`, id)
	source, err := scanSource(row)
	if errors.Is(err, pgx.ErrNoRows) {
		return Source{}, ErrNotFound
	}
	return source, err
}

// ListSources returns matching entries and the total count.
func (r *Repository) ListSources(ctx context.Context, filter SourceFilter) ([]Source, int, error) {
	filter.Limit = ClampLimit(filter.Limit)
	if filter.Offset < 0 {
		filter.Offset = 0
	}
	where := `where ($1 = '' or status = $1)
		and ($2 = '' or source_type = $2)
		and ($3 = '' or domain = $3)`
	rows, err := r.pool.Query(ctx, `select `+sourceColumns+` from ingest.sources `+where+`
		order by created_at desc limit $4 offset $5`,
		filter.Status, filter.SourceType, filter.Domain, filter.Limit, filter.Offset)
	if err != nil {
		return nil, 0, err
	}
	defer rows.Close()
	items := make([]Source, 0)
	for rows.Next() {
		source, err := scanSource(rows)
		if err != nil {
			return nil, 0, err
		}
		items = append(items, source)
	}
	if err := rows.Err(); err != nil {
		return nil, 0, err
	}
	var total int
	if err := r.pool.QueryRow(ctx, `select count(*) from ingest.sources `+where,
		filter.Status, filter.SourceType, filter.Domain).Scan(&total); err != nil {
		return nil, 0, err
	}
	return items, total, nil
}

// UpdateSource replaces mutable source fields and records a config version.
func (r *Repository) UpdateSource(ctx context.Context, source Source, updatedBy string) (Source, error) {
	if err := ValidateSource(&source); err != nil {
		return Source{}, err
	}
	metadata, _ := json.Marshal(source.Metadata)

	tx, err := r.pool.Begin(ctx)
	if err != nil {
		return Source{}, err
	}
	defer func() { _ = tx.Rollback(ctx) }()

	row := tx.QueryRow(ctx, `
		update ingest.sources set
			name = $2, base_url = $3, source_type = $4, domain = $5, language = nullif($6, ''),
			crawl_frequency_seconds = $7, priority = $8, robots_policy = $9,
			crawl_enabled = $10, status = $11, metadata = $12::jsonb,
			next_crawl_at = case
				when $11 = 'active' and $10 then coalesce(next_crawl_at, now())
				else null
			end
		where id = $1::uuid
		returning `+sourceColumns,
		source.ID, source.Name, source.BaseURL, source.SourceType, source.Domain, source.Language,
		source.CrawlFrequencySeconds, source.Priority, source.RobotsPolicy,
		source.CrawlEnabled, source.Status, string(metadata),
	)
	updated, err := scanSource(row)
	if errors.Is(err, pgx.ErrNoRows) {
		return Source{}, ErrNotFound
	}
	if err != nil {
		return Source{}, mapPostgresError(err)
	}
	if updated.Status == StatusActive && updated.CrawlEnabled {
		if _, err := tx.Exec(ctx, `update ingest.source_health
			set state = 'unknown', consecutive_failures = 0, last_error = null
			where source_id = $1::uuid and state = 'blocked'`, updated.ID); err != nil {
			return Source{}, err
		}
	}
	if err := insertConfigVersion(ctx, tx, updated, updatedBy); err != nil {
		return Source{}, err
	}
	if err := tx.Commit(ctx); err != nil {
		return Source{}, mapPostgresError(err)
	}
	return updated, nil
}

// DeleteSource removes a source and cascades its health, scores, and history.
func (r *Repository) DeleteSource(ctx context.Context, id string) error {
	command, err := r.pool.Exec(ctx, `delete from ingest.sources where id = $1::uuid`, id)
	if err != nil {
		return err
	}
	if command.RowsAffected() == 0 {
		return ErrNotFound
	}
	return nil
}

// CreateCandidate inserts a discovered candidate unless its base URL already
// exists. It returns the existing row and created=false on conflict.
func (r *Repository) CreateCandidate(ctx context.Context, candidate Candidate, createdBy string) (Source, bool, error) {
	source := Source{
		Name:         candidate.Name,
		BaseURL:      candidate.BaseURL,
		SourceType:   candidate.SourceType,
		Domain:       candidate.Domain,
		Language:     candidate.Language,
		Priority:     5,
		RobotsPolicy: RobotsRespect,
		CrawlEnabled: false,
		Status:       StatusCandidate,
		Metadata:     candidate.Metadata,
	}
	if source.Metadata == nil {
		source.Metadata = map[string]any{}
	}
	source.Metadata["discovery_score"] = candidate.Score
	source.Metadata["discovery_evidence"] = candidate.Evidence
	if err := ValidateSource(&source); err != nil {
		return Source{}, false, err
	}

	metadata, _ := json.Marshal(source.Metadata)
	tx, err := r.pool.Begin(ctx)
	if err != nil {
		return Source{}, false, err
	}
	defer func() { _ = tx.Rollback(ctx) }()
	row := tx.QueryRow(ctx, `
		insert into ingest.sources
			(name, base_url, source_type, domain, language, priority, robots_policy,
			 crawl_enabled, status, next_crawl_at, metadata)
		values ($1, $2, $3, $4, nullif($5, ''), $6, $7, false, $8, null, $9::jsonb)
		on conflict (base_url) do nothing
		returning `+sourceColumns,
		source.Name, source.BaseURL, source.SourceType, source.Domain, source.Language,
		source.Priority, source.RobotsPolicy, source.Status, string(metadata),
	)
	created, err := scanSource(row)
	if errors.Is(err, pgx.ErrNoRows) {
		// Existing source, active or otherwise.
		existing, getErr := r.GetSourceByBaseURL(ctx, source.BaseURL)
		if getErr != nil {
			return Source{}, false, getErr
		}
		return existing, false, nil
	}
	if err != nil {
		return Source{}, false, mapPostgresError(err)
	}
	if _, err := tx.Exec(ctx, `insert into ingest.source_health (source_id) values ($1::uuid)`, created.ID); err != nil {
		return Source{}, false, err
	}
	if err := insertConfigVersion(ctx, tx, created, createdBy); err != nil {
		return Source{}, false, err
	}
	if err := tx.Commit(ctx); err != nil {
		return Source{}, false, mapPostgresError(err)
	}
	return created, true, nil
}

// GetSourceByBaseURL is used by candidate deduplication.
func (r *Repository) GetSourceByBaseURL(ctx context.Context, baseURL string) (Source, error) {
	row := r.pool.QueryRow(ctx, `select `+sourceColumns+` from ingest.sources where base_url = $1`, baseURL)
	source, err := scanSource(row)
	if errors.Is(err, pgx.ErrNoRows) {
		return Source{}, ErrNotFound
	}
	return source, err
}

// DueSources returns active sources whose next_crawl_at is due, bounded by
// domain policy. A domain never contributes more than max_concurrency rows.
func (r *Repository) DueSources(ctx context.Context, now time.Time, limit int) ([]Source, error) {
	limit = ClampLimit(limit)
	rows, err := r.pool.Query(ctx, `select `+sourceColumns+` from ingest.sources s
		where s.status = 'active' and s.crawl_enabled
		  and not exists (
		    select 1 from ingest.source_health h where h.source_id = s.id and h.state = 'blocked'
		  )
		  and (s.next_crawl_at is null or s.next_crawl_at <= $1)
		order by s.next_crawl_at nulls first, s.priority desc
		limit $2`, now.UTC(), limit*4)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	candidates := make([]Source, 0)
	for rows.Next() {
		source, err := scanSource(rows)
		if err != nil {
			return nil, err
		}
		candidates = append(candidates, source)
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}

	policies, err := r.ListDomainPolicies(ctx)
	if err != nil {
		return nil, err
	}
	policyByDomain := make(map[string]DomainPolicy, len(policies))
	for _, policy := range policies {
		policyByDomain[policy.Domain] = policy
	}

	result := make([]Source, 0, limit)
	perDomain := map[string]int{}
	for _, source := range candidates {
		policy, ok := policyByDomain[source.Domain]
		if ok && (!policy.Allowed || policy.RobotsPolicy == RobotsDeny) {
			continue
		}
		allowedConcurrency := 1
		if ok {
			allowedConcurrency = policy.MaxConcurrency
			if source.LastCrawledAt != nil && now.Before(source.LastCrawledAt.Add(domainFloor(policy))) {
				continue
			}
		}
		if perDomain[source.Domain] >= allowedConcurrency {
			continue
		}
		result = append(result, source)
		perDomain[source.Domain]++
		if len(result) >= limit {
			break
		}
	}
	return result, nil
}

// RecordOutcome updates health, history, and the next adaptive crawl time.
func (r *Repository) RecordOutcome(ctx context.Context, id string, outcome CrawlOutcome) (Source, SourceHealth, error) {
	if err := ValidateCrawlOutcome(&outcome); err != nil {
		return Source{}, SourceHealth{}, err
	}
	now := time.Now().UTC()

	tx, err := r.pool.Begin(ctx)
	if err != nil {
		return Source{}, SourceHealth{}, err
	}
	defer func() { _ = tx.Rollback(ctx) }()

	source, err := getSourceTx(ctx, tx, id)
	if errors.Is(err, pgx.ErrNoRows) {
		return Source{}, SourceHealth{}, ErrNotFound
	}
	if err != nil {
		return Source{}, SourceHealth{}, err
	}
	health, err := getHealthTx(ctx, tx, id)
	if err != nil && !errors.Is(err, pgx.ErrNoRows) {
		return Source{}, SourceHealth{}, err
	}
	if health.State == "" {
		health.State = "unknown"
	}
	var policy *DomainPolicy
	policyRow := tx.QueryRow(ctx, `select `+policyColumns+` from ingest.source_domain_policies where domain = $1`, source.Domain)
	p, err := scanPolicy(policyRow)
	if err == nil {
		policy = &p
	} else if !errors.Is(err, pgx.ErrNoRows) {
		return Source{}, SourceHealth{}, err
	}

	switch outcome.Status {
	case "success":
		health.State = "healthy"
		health.ConsecutiveFailures = 0
		health.TotalSuccesses++
		health.LastSuccessAt = &now
		health.LastError = ""
		health.LastRetryAfterSeconds = nil
	case "failure":
		health.ConsecutiveFailures++
		health.TotalFailures++
		health.LastFailureAt = &now
		health.LastError = outcome.Error
		health.LastRetryAfterSeconds = outcome.RetryAfterSeconds
		health.State = "degraded"
		if health.ConsecutiveFailures >= 3 {
			health.State = "failing"
		}
		if looksBlocked(outcome.Error) {
			health.State = "blocked"
		}
	}
	health.SourceID = id
	health.UpdatedAt = now

	schedule := DefaultScheduleConfig()
	next := schedule.NextCrawl(source, health, policy, outcome.RetryAfterSeconds, now)

	if _, err := tx.Exec(ctx, `
		insert into ingest.source_crawl_history
			(source_id, started_at, finished_at, status, http_status, items_found, error, trigger)
		values ($1::uuid, $2, $2, $3, $4, $5, nullif($6, ''), $7)`,
		id, now, outcome.Status, outcome.HTTPStatus, outcome.ItemsFound, outcome.Error, outcome.Trigger); err != nil {
		return Source{}, SourceHealth{}, err
	}

	if _, err := tx.Exec(ctx, `
		insert into ingest.source_health
			(source_id, state, consecutive_failures, total_successes, total_failures,
			 last_success_at, last_failure_at, last_error, last_retry_after_seconds, updated_at)
		values ($1::uuid, $2, $3, $4, $5, $6, $7, nullif($8, ''), $9, $10)
		on conflict (source_id) do update set
			state = excluded.state,
			consecutive_failures = excluded.consecutive_failures,
			total_successes = excluded.total_successes,
			total_failures = excluded.total_failures,
			last_success_at = excluded.last_success_at,
			last_failure_at = excluded.last_failure_at,
			last_error = excluded.last_error,
			last_retry_after_seconds = excluded.last_retry_after_seconds,
			updated_at = excluded.updated_at`,
		id, health.State, health.ConsecutiveFailures, health.TotalSuccesses, health.TotalFailures,
		health.LastSuccessAt, health.LastFailureAt, health.LastError, health.LastRetryAfterSeconds, health.UpdatedAt); err != nil {
		return Source{}, SourceHealth{}, err
	}

	row := tx.QueryRow(ctx, `update ingest.sources
		set last_crawled_at = $2, next_crawl_at = $3
		where id = $1::uuid returning `+sourceColumns, id, now, next)
	updated, err := scanSource(row)
	if err != nil {
		return Source{}, SourceHealth{}, err
	}
	if err := tx.Commit(ctx); err != nil {
		return Source{}, SourceHealth{}, err
	}
	return updated, health, nil
}

// SaveScore persists an evaluation and updates the source's current score.
func (r *Repository) SaveScore(ctx context.Context, score Score) (Score, error) {
	weights, _ := json.Marshal(score.Weights)
	details, _ := json.Marshal(score.Details)
	d := score.Dimensions

	tx, err := r.pool.Begin(ctx)
	if err != nil {
		return Score{}, err
	}
	defer func() { _ = tx.Rollback(ctx) }()

	row := tx.QueryRow(ctx, `
		insert into ingest.source_scores
			(source_id, model_version, evaluated_at, authority, originality, historical_accuracy,
			 topical_relevance, publication_frequency, freshness, transparency, corrections,
			 composite, weights, details)
		values ($1::uuid, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13::jsonb, $14::jsonb)
		returning id::text, source_id::text, model_version, evaluated_at,
			authority::float8, originality::float8, historical_accuracy::float8,
			topical_relevance::float8, publication_frequency::float8, freshness::float8,
			transparency::float8, corrections::float8, composite::float8, weights, details`,
		score.SourceID, score.ModelVersion, score.EvaluatedAt,
		d.Authority, d.Originality, d.HistoricalAccuracy, d.TopicalRelevance,
		d.PublicationFrequency, d.Freshness, d.Transparency, d.Corrections,
		score.Composite, string(weights), string(details),
	)
	saved, err := scanScore(row)
	if err != nil {
		return Score{}, err
	}
	if _, err := tx.Exec(ctx, `update ingest.sources set reliability_score = $2 where id = $1::uuid`,
		score.SourceID, saved.Composite); err != nil {
		return Score{}, err
	}
	if err := tx.Commit(ctx); err != nil {
		return Score{}, err
	}
	return saved, nil
}

// LatestScore returns the newest persisted score for a source.
func (r *Repository) LatestScore(ctx context.Context, id string) (Score, error) {
	row := r.pool.QueryRow(ctx, `
		select id::text, source_id::text, model_version, evaluated_at,
			authority::float8, originality::float8, historical_accuracy::float8,
			topical_relevance::float8, publication_frequency::float8, freshness::float8,
			transparency::float8, corrections::float8, composite::float8, weights, details
		from ingest.source_scores where source_id = $1::uuid
		order by evaluated_at desc limit 1`, id)
	score, err := scanScore(row)
	if errors.Is(err, pgx.ErrNoRows) {
		return Score{}, ErrNotFound
	}
	return score, err
}

// Health returns the health projection for a source.
func (r *Repository) Health(ctx context.Context, id string) (SourceHealth, error) {
	row := r.pool.QueryRow(ctx, `select `+healthColumns+` from ingest.source_health where source_id = $1::uuid`, id)
	health, err := scanHealth(row)
	if errors.Is(err, pgx.ErrNoRows) {
		return SourceHealth{}, ErrNotFound
	}
	return health, err
}

// ListHealth returns all health projections for dashboards.
func (r *Repository) ListHealth(ctx context.Context) ([]SourceHealth, error) {
	rows, err := r.pool.Query(ctx, `select `+healthColumns+` from ingest.source_health order by updated_at desc`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	items := make([]SourceHealth, 0)
	for rows.Next() {
		item, err := scanHealth(rows)
		if err != nil {
			return nil, err
		}
		items = append(items, item)
	}
	return items, rows.Err()
}

// CrawlHistory returns the most recent attempts for a source.
func (r *Repository) CrawlHistory(ctx context.Context, id string, limit int) ([]CrawlHistory, error) {
	limit = ClampLimit(limit)
	rows, err := r.pool.Query(ctx, `
		select id, source_id::text, started_at, finished_at, status, http_status,
			items_found, coalesce(error, ''), trigger, created_at
		from ingest.source_crawl_history
		where source_id = $1::uuid order by started_at desc limit $2`, id, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	items := make([]CrawlHistory, 0)
	for rows.Next() {
		var item CrawlHistory
		if err := rows.Scan(&item.ID, &item.SourceID, &item.StartedAt, &item.FinishedAt,
			&item.Status, &item.HTTPStatus, &item.ItemsFound, &item.Error, &item.Trigger,
			&item.CreatedAt); err != nil {
			return nil, err
		}
		items = append(items, item)
	}
	return items, rows.Err()
}

// UpsertDomainPolicy inserts or updates a domain policy.
func (r *Repository) UpsertDomainPolicy(ctx context.Context, policy DomainPolicy) (DomainPolicy, error) {
	if err := ValidateDomainPolicy(&policy); err != nil {
		return DomainPolicy{}, err
	}
	metadata, _ := json.Marshal(policy.Metadata)
	row := r.pool.QueryRow(ctx, `
		insert into ingest.source_domain_policies
			(domain, max_concurrency, requests_per_minute, crawl_delay_ms, robots_policy, allowed, notes, metadata)
		values ($1, $2, $3, $4, $5, $6, nullif($7, ''), $8::jsonb)
		on conflict (domain) do update set
			max_concurrency = excluded.max_concurrency,
			requests_per_minute = excluded.requests_per_minute,
			crawl_delay_ms = excluded.crawl_delay_ms,
			robots_policy = excluded.robots_policy,
			allowed = excluded.allowed,
			notes = excluded.notes,
			metadata = excluded.metadata
		returning `+policyColumns,
		policy.Domain, policy.MaxConcurrency, policy.RequestsPerMinute, policy.CrawlDelayMS,
		policy.RobotsPolicy, policy.Allowed, policy.Notes, string(metadata))
	return scanPolicy(row)
}

func (r *Repository) GetDomainPolicy(ctx context.Context, domain string) (DomainPolicy, error) {
	row := r.pool.QueryRow(ctx, `select `+policyColumns+` from ingest.source_domain_policies where domain = $1`, domain)
	policy, err := scanPolicy(row)
	if errors.Is(err, pgx.ErrNoRows) {
		return DomainPolicy{}, ErrNotFound
	}
	return policy, err
}

func (r *Repository) ListDomainPolicies(ctx context.Context) ([]DomainPolicy, error) {
	rows, err := r.pool.Query(ctx, `select `+policyColumns+` from ingest.source_domain_policies order by domain`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	items := make([]DomainPolicy, 0)
	for rows.Next() {
		item, err := scanPolicy(rows)
		if err != nil {
			return nil, err
		}
		items = append(items, item)
	}
	return items, rows.Err()
}

func (r *Repository) DeleteDomainPolicy(ctx context.Context, domain string) error {
	command, err := r.pool.Exec(ctx, `delete from ingest.source_domain_policies where domain = $1`, domain)
	if err != nil {
		return err
	}
	if command.RowsAffected() == 0 {
		return ErrNotFound
	}
	return nil
}

// RecordDiscovery stores one discovery run and its candidates.
func (r *Repository) RecordDiscovery(ctx context.Context, mechanism string, input map[string]any, candidates []Candidate, registered int, runErr error) (string, error) {
	inputJSON, _ := json.Marshal(input)
	candidatesJSON, _ := json.Marshal(candidates)
	status := "completed"
	errorText := ""
	if runErr != nil {
		status = "failed"
		errorText = runErr.Error()
	}
	var id string
	err := r.pool.QueryRow(ctx, `
		insert into ingest.source_discovery_history
			(mechanism, input, candidates, candidate_count, registered_count, status, error)
		values ($1, $2::jsonb, $3::jsonb, $4, $5, $6, nullif($7, ''))
		returning id::text`,
		mechanism, string(inputJSON), string(candidatesJSON), len(candidates), registered, status, errorText).Scan(&id)
	return id, err
}

func (r *Repository) ListDiscoveries(ctx context.Context, limit int) ([]DiscoveryRun, error) {
	limit = ClampLimit(limit)
	rows, err := r.pool.Query(ctx, `
		select id::text, mechanism, input, candidates, candidate_count, registered_count, status,
			coalesce(error, ''), created_at
		from ingest.source_discovery_history order by created_at desc limit $1`, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	items := make([]DiscoveryRun, 0)
	for rows.Next() {
		var (
			run        DiscoveryRun
			inputRaw   []byte
			candidates []byte
		)
		if err := rows.Scan(&run.ID, &run.Mechanism, &inputRaw, &candidates, &run.CandidateCount,
			&run.RegisteredCount, &run.Status, &run.Error, &run.CreatedAt); err != nil {
			return nil, err
		}
		_ = json.Unmarshal(inputRaw, &run.Input)
		_ = json.Unmarshal(candidates, &run.Candidates)
		items = append(items, run)
	}
	return items, rows.Err()
}

// ConfigVersions returns the immutable configuration snapshots for a source.
func (r *Repository) ConfigVersions(ctx context.Context, id string) ([]map[string]any, error) {
	rows, err := r.pool.Query(ctx, `
		select version, config, coalesce(created_by, ''), created_at
		from ingest.source_config_versions where source_id = $1::uuid
		order by version desc`, id)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	items := make([]map[string]any, 0)
	for rows.Next() {
		var version int
		var configRaw []byte
		var createdBy string
		var createdAt time.Time
		if err := rows.Scan(&version, &configRaw, &createdBy, &createdAt); err != nil {
			return nil, err
		}
		var config map[string]any
		_ = json.Unmarshal(configRaw, &config)
		items = append(items, map[string]any{
			"version": version, "config": config, "created_by": createdBy, "created_at": createdAt,
		})
	}
	return items, rows.Err()
}

// Stats aggregates source counts for the metrics refresher.
func (r *Repository) Stats(ctx context.Context) (Stats, error) {
	stats := Stats{
		ByTypeStatus:      map[string]int{},
		HealthByState:     map[string]int{},
		ReliabilityByType: map[string]float64{},
	}
	rows, err := r.pool.Query(ctx, `select status, source_type, count(*) from ingest.sources group by 1, 2`)
	if err != nil {
		return stats, err
	}
	for rows.Next() {
		var status, sourceType string
		var count int
		if err := rows.Scan(&status, &sourceType, &count); err != nil {
			rows.Close()
			return stats, err
		}
		stats.ByTypeStatus[sourceType+"|"+status] = count
	}
	rows.Close()
	if err := rows.Err(); err != nil {
		return stats, err
	}

	if err := r.pool.QueryRow(ctx, `select count(*) from ingest.sources
		where status = 'active' and crawl_enabled and (next_crawl_at is null or next_crawl_at <= now())`).Scan(&stats.Due); err != nil {
		return stats, err
	}

	healthRows, err := r.pool.Query(ctx, `select state, count(*) from ingest.source_health group by 1`)
	if err != nil {
		return stats, err
	}
	for healthRows.Next() {
		var state string
		var count int
		if err := healthRows.Scan(&state, &count); err != nil {
			healthRows.Close()
			return stats, err
		}
		stats.HealthByState[state] = count
	}
	healthRows.Close()
	if err := healthRows.Err(); err != nil {
		return stats, err
	}

	reliabilityRows, err := r.pool.Query(ctx, `select source_type, avg(reliability_score)::float8
		from ingest.sources where reliability_score is not null group by 1`)
	if err != nil {
		return stats, err
	}
	defer reliabilityRows.Close()
	for reliabilityRows.Next() {
		var sourceType string
		var avg float64
		if err := reliabilityRows.Scan(&sourceType, &avg); err != nil {
			return stats, err
		}
		stats.ReliabilityByType[sourceType] = avg
	}
	return stats, reliabilityRows.Err()
}

func insertConfigVersion(ctx context.Context, tx pgx.Tx, source Source, createdBy string) error {
	config, _ := json.Marshal(source)
	var version int
	if err := tx.QueryRow(ctx, `select coalesce(max(version), 0) + 1 from ingest.source_config_versions
		where source_id = $1::uuid`, source.ID).Scan(&version); err != nil {
		return err
	}
	_, err := tx.Exec(ctx, `insert into ingest.source_config_versions
		(source_id, version, config, created_by) values ($1::uuid, $2, $3::jsonb, nullif($4, ''))`,
		source.ID, version, string(config), createdBy)
	return err
}

func getSourceTx(ctx context.Context, tx pgx.Tx, id string) (Source, error) {
	row := tx.QueryRow(ctx, `select `+sourceColumns+` from ingest.sources where id = $1::uuid`, id)
	return scanSource(row)
}

func getHealthTx(ctx context.Context, tx pgx.Tx, id string) (SourceHealth, error) {
	row := tx.QueryRow(ctx, `select `+healthColumns+` from ingest.source_health where source_id = $1::uuid`, id)
	return scanHealth(row)
}

type rowScanner interface {
	Scan(dest ...any) error
}

func scanSource(row rowScanner) (Source, error) {
	var (
		source      Source
		language    string
		crawlFreq   *int
		lastCrawled *time.Time
		nextCrawl   *time.Time
		reliability *float64
		metadata    []byte
	)
	if err := row.Scan(&source.ID, &source.Name, &source.BaseURL, &source.SourceType, &source.Domain,
		&language, &crawlFreq, &source.Priority, &source.RobotsPolicy, &source.CrawlEnabled,
		&source.Status, &lastCrawled, &nextCrawl, &reliability, &metadata,
		&source.CreatedAt, &source.UpdatedAt); err != nil {
		return Source{}, err
	}
	source.Language = language
	source.CrawlFrequencySeconds = crawlFreq
	source.LastCrawledAt = lastCrawled
	source.NextCrawlAt = nextCrawl
	source.ReliabilityScore = reliability
	source.Metadata = map[string]any{}
	_ = json.Unmarshal(metadata, &source.Metadata)
	return source, nil
}

func scanPolicy(row rowScanner) (DomainPolicy, error) {
	var (
		policy   DomainPolicy
		metadata []byte
	)
	if err := row.Scan(&policy.Domain, &policy.MaxConcurrency, &policy.RequestsPerMinute,
		&policy.CrawlDelayMS, &policy.RobotsPolicy, &policy.Allowed, &policy.Notes,
		&metadata, &policy.CreatedAt, &policy.UpdatedAt); err != nil {
		return DomainPolicy{}, err
	}
	policy.Metadata = map[string]any{}
	_ = json.Unmarshal(metadata, &policy.Metadata)
	return policy, nil
}

func scanHealth(row rowScanner) (SourceHealth, error) {
	var health SourceHealth
	if err := row.Scan(&health.SourceID, &health.State, &health.ConsecutiveFailures,
		&health.TotalSuccesses, &health.TotalFailures, &health.LastSuccessAt,
		&health.LastFailureAt, &health.LastError, &health.LastRetryAfterSeconds,
		&health.UpdatedAt); err != nil {
		return SourceHealth{}, err
	}
	return health, nil
}

func scanScore(row rowScanner) (Score, error) {
	var (
		score   Score
		weights []byte
		details []byte
	)
	if err := row.Scan(&score.ID, &score.SourceID, &score.ModelVersion, &score.EvaluatedAt,
		&score.Dimensions.Authority, &score.Dimensions.Originality,
		&score.Dimensions.HistoricalAccuracy, &score.Dimensions.TopicalRelevance,
		&score.Dimensions.PublicationFrequency, &score.Dimensions.Freshness,
		&score.Dimensions.Transparency, &score.Dimensions.Corrections,
		&score.Composite, &weights, &details); err != nil {
		return Score{}, err
	}
	_ = json.Unmarshal(weights, &score.Weights)
	score.Details = map[string]any{}
	_ = json.Unmarshal(details, &score.Details)
	return score, nil
}

func mapPostgresError(err error) error {
	var pgErr *pgconn.PgError
	if errors.As(err, &pgErr) && pgErr.Code == "23505" {
		return ErrConflict
	}
	return err
}

func looksBlocked(message string) bool {
	message = strings.ToLower(message)
	return strings.Contains(message, "robots") ||
		strings.Contains(message, "policy") ||
		strings.Contains(message, "blocked")
}
