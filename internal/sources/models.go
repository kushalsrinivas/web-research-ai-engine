package sources

import "time"

// Source types accepted by the registry. They describe how a source is
// discovered and crawled, not how trustworthy it is.
const (
	TypeRSS          = "rss"
	TypeWebsite      = "website"
	TypeNews         = "news"
	TypeGovernment   = "government"
	TypeResearch     = "research"
	TypeFinancial    = "financial"
	TypePressRelease = "press_release"
	TypePDF          = "pdf"
	TypeDataset      = "dataset"
	TypeForum        = "forum"
)

var allowedSourceTypes = map[string]struct{}{
	TypeRSS: {}, TypeWebsite: {}, TypeNews: {}, TypeGovernment: {}, TypeResearch: {},
	TypeFinancial: {}, TypePressRelease: {}, TypePDF: {}, TypeDataset: {}, TypeForum: {},
}

const (
	StatusActive    = "active"
	StatusCandidate = "candidate"
	StatusDisabled  = "disabled"
	StatusRejected  = "rejected"
)

var allowedStatuses = map[string]struct{}{
	StatusActive: {}, StatusCandidate: {}, StatusDisabled: {}, StatusRejected: {},
}

const (
	RobotsRespect = "respect"
	RobotsAllow   = "allow"
	RobotsDeny    = "deny"
)

var allowedRobotsPolicies = map[string]struct{}{
	RobotsRespect: {}, RobotsAllow: {}, RobotsDeny: {},
}

// Source is one registry entry. A source is a discovery target, not an
// authority; reliability scores are inputs to scheduling and presentation.
type Source struct {
	ID                    string         `json:"id"`
	Name                  string         `json:"name"`
	BaseURL               string         `json:"base_url"`
	SourceType            string         `json:"source_type"`
	Domain                string         `json:"domain"`
	Language              string         `json:"language,omitempty"`
	CrawlFrequencySeconds *int           `json:"crawl_frequency_seconds,omitempty"`
	Priority              int            `json:"priority"`
	RobotsPolicy          string         `json:"robots_policy"`
	CrawlEnabled          bool           `json:"crawl_enabled"`
	Status                string         `json:"status"`
	LastCrawledAt         *time.Time     `json:"last_crawled_at,omitempty"`
	NextCrawlAt           *time.Time     `json:"next_crawl_at,omitempty"`
	ReliabilityScore      *float64       `json:"reliability_score,omitempty"`
	Metadata              map[string]any `json:"metadata"`
	CreatedAt             time.Time      `json:"created_at"`
	UpdatedAt             time.Time      `json:"updated_at"`
}

// SourcePatch is a partial update. Nil fields are unchanged.
type SourcePatch struct {
	Name                  *string        `json:"name,omitempty"`
	BaseURL               *string        `json:"base_url,omitempty"`
	SourceType            *string        `json:"source_type,omitempty"`
	Domain                *string        `json:"domain,omitempty"`
	Language              *string        `json:"language,omitempty"`
	CrawlFrequencySeconds *int           `json:"crawl_frequency_seconds,omitempty"`
	Priority              *int           `json:"priority,omitempty"`
	RobotsPolicy          *string        `json:"robots_policy,omitempty"`
	CrawlEnabled          *bool          `json:"crawl_enabled,omitempty"`
	Status                *string        `json:"status,omitempty"`
	Metadata              map[string]any `json:"metadata,omitempty"`
}

// SourceFilter selects registry rows.
type SourceFilter struct {
	Status     string
	SourceType string
	Domain     string
	Limit      int
	Offset     int
}

// DomainPolicy is the enforcement point for per-domain politeness.
type DomainPolicy struct {
	Domain            string         `json:"domain"`
	MaxConcurrency    int            `json:"max_concurrency"`
	RequestsPerMinute int            `json:"requests_per_minute"`
	CrawlDelayMS      int            `json:"crawl_delay_ms"`
	RobotsPolicy      string         `json:"robots_policy"`
	Allowed           bool           `json:"allowed"`
	Notes             string         `json:"notes,omitempty"`
	Metadata          map[string]any `json:"metadata"`
	CreatedAt         time.Time      `json:"created_at"`
	UpdatedAt         time.Time      `json:"updated_at"`
}

// SourceHealth is the current crawl-health projection for a source.
type SourceHealth struct {
	SourceID              string     `json:"source_id"`
	State                 string     `json:"state"`
	ConsecutiveFailures   int        `json:"consecutive_failures"`
	TotalSuccesses        int64      `json:"total_successes"`
	TotalFailures         int64      `json:"total_failures"`
	LastSuccessAt         *time.Time `json:"last_success_at,omitempty"`
	LastFailureAt         *time.Time `json:"last_failure_at,omitempty"`
	LastError             string     `json:"last_error,omitempty"`
	LastRetryAfterSeconds *int       `json:"last_retry_after_seconds,omitempty"`
	UpdatedAt             time.Time  `json:"updated_at"`
}

// Dimensions is the source-quality model. Each value is in [0,1].
type Dimensions struct {
	Authority            float64 `json:"authority"`
	Originality          float64 `json:"originality"`
	HistoricalAccuracy   float64 `json:"historical_accuracy"`
	TopicalRelevance     float64 `json:"topical_relevance"`
	PublicationFrequency float64 `json:"publication_frequency"`
	Freshness            float64 `json:"freshness"`
	Transparency         float64 `json:"transparency"`
	Corrections          float64 `json:"corrections"`
}

// Score is a persisted evaluation. ModelVersion and EvaluatedAt make scores
// reproducible and explainable.
type Score struct {
	ID           string         `json:"id"`
	SourceID     string         `json:"source_id"`
	ModelVersion string         `json:"model_version"`
	EvaluatedAt  time.Time      `json:"evaluated_at"`
	Dimensions   Dimensions     `json:"dimensions"`
	Composite    float64        `json:"composite"`
	Weights      Weights        `json:"weights"`
	Details      map[string]any `json:"details"`
}

// Weights must sum to 1.
type Weights struct {
	Authority            float64 `json:"authority"`
	Originality          float64 `json:"originality"`
	HistoricalAccuracy   float64 `json:"historical_accuracy"`
	TopicalRelevance     float64 `json:"topical_relevance"`
	PublicationFrequency float64 `json:"publication_frequency"`
	Freshness            float64 `json:"freshness"`
	Transparency         float64 `json:"transparency"`
	Corrections          float64 `json:"corrections"`
}

// CrawlOutcome is reported by a crawler after an attempt.
type CrawlOutcome struct {
	Status            string `json:"status"`
	HTTPStatus        *int   `json:"http_status,omitempty"`
	ItemsFound        int    `json:"items_found"`
	Error             string `json:"error,omitempty"`
	RetryAfterSeconds *int   `json:"retry_after_seconds,omitempty"`
	Trigger           string `json:"trigger,omitempty"`
}

// Candidate is a discovery result that can be promoted into the registry.
type Candidate struct {
	Name       string         `json:"name"`
	BaseURL    string         `json:"base_url"`
	SourceType string         `json:"source_type"`
	Domain     string         `json:"domain"`
	Language   string         `json:"language,omitempty"`
	Score      float64        `json:"score"`
	Evidence   map[string]any `json:"evidence,omitempty"`
	Metadata   map[string]any `json:"metadata,omitempty"`
}

// DiscoveryResult is returned by every discovery mechanism.
type DiscoveryResult struct {
	ID         string      `json:"discovery_id"`
	Mechanism  string      `json:"mechanism"`
	Candidates []Candidate `json:"candidates"`
	Registered []Source    `json:"registered,omitempty"`
}

// DiscoveryRun is a stored discovery audit record.
type DiscoveryRun struct {
	ID              string         `json:"id"`
	Mechanism       string         `json:"mechanism"`
	Input           map[string]any `json:"input"`
	Candidates      []Candidate    `json:"candidates"`
	CandidateCount  int            `json:"candidate_count"`
	RegisteredCount int            `json:"registered_count"`
	Status          string         `json:"status"`
	Error           string         `json:"error,omitempty"`
	CreatedAt       time.Time      `json:"created_at"`
}

// CrawlHistory is an append-only record of crawl attempts.
type CrawlHistory struct {
	ID         int64      `json:"id"`
	SourceID   string     `json:"source_id"`
	StartedAt  time.Time  `json:"started_at"`
	FinishedAt *time.Time `json:"finished_at,omitempty"`
	Status     string     `json:"status"`
	HTTPStatus *int       `json:"http_status,omitempty"`
	ItemsFound int        `json:"items_found"`
	Error      string     `json:"error,omitempty"`
	Trigger    string     `json:"trigger"`
	CreatedAt  time.Time  `json:"created_at"`
}

// Stats backs the dashboard metrics refresh.
type Stats struct {
	ByTypeStatus      map[string]int
	Due               int
	HealthByState     map[string]int
	ReliabilityByType map[string]float64
}
