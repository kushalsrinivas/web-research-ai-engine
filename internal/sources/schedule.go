package sources

import (
	"math"
	"time"
)

// ScheduleConfig contains the configurable starting points for adaptive
// scheduling. Per-source overrides and domain policies constrain the result.
type ScheduleConfig struct {
	BaseIntervals map[string]time.Duration
	MinInterval   time.Duration
	MaxBackoff    time.Duration
}

// DefaultScheduleConfig reflects the Phase 2 starting values, not mandates.
func DefaultScheduleConfig() ScheduleConfig {
	return ScheduleConfig{
		BaseIntervals: map[string]time.Duration{
			TypeNews:         10 * time.Minute,
			TypeRSS:          15 * time.Minute,
			TypePressRelease: 45 * time.Minute,
			TypeFinancial:    time.Hour,
			TypeGovernment:   3 * time.Hour,
			TypeResearch:     12 * time.Hour,
			TypeDataset:      12 * time.Hour,
			TypeForum:        time.Hour,
			TypePDF:          24 * time.Hour,
			TypeWebsite:      24 * time.Hour,
		},
		MinInterval: time.Minute,
		MaxBackoff:  7 * 24 * time.Hour,
	}
}

// InitialNextCrawl sets active, enabled sources to crawl immediately. Candidates
// and disabled sources stay unscheduled until promoted.
func InitialNextCrawl(s Source, now time.Time) *time.Time {
	if s.Status != StatusActive || !s.CrawlEnabled || s.RobotsPolicy == RobotsDeny {
		return nil
	}
	t := now.UTC()
	return &t
}

// NextCrawl computes the next attempt time. The interval grows with
// consecutive failures, honors Retry-After, and never goes below a domain
// policy floor.
func (c ScheduleConfig) NextCrawl(s Source, health SourceHealth, policy *DomainPolicy, retryAfter *int, now time.Time) *time.Time {
	if s.Status != StatusActive || !s.CrawlEnabled || s.RobotsPolicy == RobotsDeny {
		return nil
	}
	if health.State == "blocked" {
		return nil
	}
	if policy != nil {
		if !policy.Allowed || policy.RobotsPolicy == RobotsDeny {
			return nil
		}
	}

	base := c.intervalFor(s)
	if s.CrawlFrequencySeconds == nil {
		base = time.Duration(float64(base) * priorityFactor(s.Priority) * reliabilityFactor(s.ReliabilityScore))
	}
	if policy != nil {
		floor := domainFloor(*policy)
		if floor > base {
			base = floor
		}
	}
	if base < c.MinInterval {
		base = c.MinInterval
	}

	if health.ConsecutiveFailures > 0 {
		exponent := math.Min(float64(health.ConsecutiveFailures), 8)
		base = time.Duration(float64(base) * math.Pow(2, exponent))
	}
	if retryAfter != nil && *retryAfter > 0 {
		requested := time.Duration(*retryAfter) * time.Second
		if requested > base {
			base = requested
		}
	}
	if base > c.MaxBackoff {
		base = c.MaxBackoff
	}
	next := now.UTC().Add(base)
	return &next
}

func (c ScheduleConfig) intervalFor(s Source) time.Duration {
	if s.CrawlFrequencySeconds != nil && *s.CrawlFrequencySeconds >= 60 {
		return time.Duration(*s.CrawlFrequencySeconds) * time.Second
	}
	if base, ok := c.BaseIntervals[s.SourceType]; ok {
		return base
	}
	return 24 * time.Hour
}

func priorityFactor(priority int) float64 {
	if priority < 1 {
		priority = 1
	}
	if priority > 10 {
		priority = 10
	}
	// Priority 10 polls 25% sooner than neutral; priority 1 polls 20% later.
	return 1.0 + 0.05*float64(5-priority)
}

func reliabilityFactor(score *float64) float64 {
	if score == nil {
		return 1.0
	}
	return 1.2 - 0.4*(*score)
}

func domainFloor(policy DomainPolicy) time.Duration {
	floor := time.Duration(policy.CrawlDelayMS) * time.Millisecond
	if policy.RequestsPerMinute > 0 {
		if perRequest := time.Minute / time.Duration(policy.RequestsPerMinute); perRequest > floor {
			floor = perRequest
		}
	}
	return floor
}
