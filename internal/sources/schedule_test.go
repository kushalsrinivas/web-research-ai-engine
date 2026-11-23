package sources

import (
	"testing"
	"time"
)

func TestNextCrawlUsesTypeAndPriority(t *testing.T) {
	now := time.Date(2026, 11, 23, 10, 0, 0, 0, time.UTC)
	config := DefaultScheduleConfig()
	source := Source{SourceType: TypeNews, Status: StatusActive, CrawlEnabled: true, Priority: 5}
	next := config.NextCrawl(source, SourceHealth{}, nil, nil, now)
	if next == nil {
		t.Fatal("expected a schedule")
	}
	if got := next.Sub(now); got != 10*time.Minute {
		t.Fatalf("interval = %s, want 10m", got)
	}

	source.Priority = 10
	next = config.NextCrawl(source, SourceHealth{}, nil, nil, now)
	if got := next.Sub(now); got != 450*time.Second {
		t.Fatalf("priority interval = %s, want 7m30s", got)
	}
}

func TestNextCrawlBackoffAndRetryAfter(t *testing.T) {
	now := time.Date(2026, 11, 23, 10, 0, 0, 0, time.UTC)
	config := DefaultScheduleConfig()
	source := Source{SourceType: TypeNews, Status: StatusActive, CrawlEnabled: true, Priority: 5}
	next := config.NextCrawl(source, SourceHealth{ConsecutiveFailures: 2}, nil, nil, now)
	if got := next.Sub(now); got != 40*time.Minute {
		t.Fatalf("backoff = %s, want 40m", got)
	}

	retryAfter := 2 * 60 * 60
	next = config.NextCrawl(source, SourceHealth{ConsecutiveFailures: 1}, nil, &retryAfter, now)
	if got := next.Sub(now); got != 2*time.Hour {
		t.Fatalf("retry-after = %s, want 2h", got)
	}
}

func TestNextCrawlDomainFloorAndManualFrequency(t *testing.T) {
	now := time.Date(2026, 11, 23, 10, 0, 0, 0, time.UTC)
	config := DefaultScheduleConfig()
	frequency := 300
	source := Source{
		SourceType: TypeWebsite, Status: StatusActive, CrawlEnabled: true,
		Priority: 5, CrawlFrequencySeconds: &frequency,
	}
	policy := &DomainPolicy{Allowed: true, MaxConcurrency: 1, RequestsPerMinute: 30, CrawlDelayMS: 600000}
	next := config.NextCrawl(source, SourceHealth{}, policy, nil, now)
	if got := next.Sub(now); got != 10*time.Minute {
		t.Fatalf("policy floor = %s, want 10m", got)
	}
}

func TestNextCrawlSkipsDisabledAndDenied(t *testing.T) {
	now := time.Now().UTC()
	config := DefaultScheduleConfig()
	for _, source := range []Source{
		{SourceType: TypeNews, Status: StatusCandidate, CrawlEnabled: false},
		{SourceType: TypeNews, Status: StatusActive, CrawlEnabled: false},
		{SourceType: TypeNews, Status: StatusActive, CrawlEnabled: true, RobotsPolicy: RobotsDeny},
	} {
		if next := config.NextCrawl(source, SourceHealth{}, nil, nil, now); next != nil {
			t.Fatalf("expected no schedule for %+v", source)
		}
	}
	active := Source{SourceType: TypeNews, Status: StatusActive, CrawlEnabled: true, Priority: 5}
	if next := config.NextCrawl(active, SourceHealth{State: "blocked"}, nil, nil, now); next != nil {
		t.Fatal("blocked sources must not be scheduled")
	}
}
