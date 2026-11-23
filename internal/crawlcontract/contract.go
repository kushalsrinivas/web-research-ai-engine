// Package crawlcontract is the wire format between the scheduler and crawler
// workers. Keep it JSON-only so producers and consumers stay independent.
package crawlcontract

import "time"

const (
	TopicFetchBulk        = "doc.fetch.bulk"
	TopicFetchInteractive = "doc.fetch.interactive"
	TopicDeadLetter       = "doc.dead_letter"
	ConsumerGroup         = "fetcher"
)

type DomainPolicy struct {
	MaxConcurrency    int    `json:"max_concurrency"`
	RequestsPerMinute int    `json:"requests_per_minute"`
	CrawlDelayMS      int    `json:"crawl_delay_ms"`
	RobotsPolicy      string `json:"robots_policy"`
	Allowed           bool   `json:"allowed"`
}

func DefaultDomainPolicy() DomainPolicy {
	return DomainPolicy{
		MaxConcurrency:    1,
		RequestsPerMinute: 30,
		CrawlDelayMS:      1000,
		RobotsPolicy:      "respect",
		Allowed:           true,
	}
}

type JobMessage struct {
	JobID              string       `json:"job_id"`
	DocumentID         string       `json:"document_id"`
	SourceID           string       `json:"source_id,omitempty"`
	URL                string       `json:"url"`
	TenantID           string       `json:"tenant_id"`
	Lane               string       `json:"lane"`
	Method             string       `json:"method"`
	Depth              int          `json:"depth"`
	DomainPolicy       DomainPolicy `json:"domain_policy"`
	SourceRobotsPolicy string       `json:"source_robots_policy"`
	EnqueuedAt         time.Time    `json:"enqueued_at"`
}

func (m JobMessage) Topic() string {
	if m.Lane == "interactive" {
		return TopicFetchInteractive
	}
	return TopicFetchBulk
}
