// Package robots parses and caches robots.txt rules.
package robots

import (
	"context"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"
)

type Rules struct {
	Disallow   []string
	Allow      []string
	CrawlDelay time.Duration
	Sitemaps   []string
}

func (r Rules) Allowed(path string) bool {
	if path == "" {
		path = "/"
	}
	bestLen := -1
	bestAllow := true
	for _, pattern := range r.Disallow {
		if matches(pattern, path) && len(pattern) > bestLen {
			bestLen, bestAllow = len(pattern), false
		}
	}
	for _, pattern := range r.Allow {
		if matches(pattern, path) && len(pattern) >= bestLen {
			bestLen, bestAllow = len(pattern), true
		}
	}
	return bestAllow
}

func matches(pattern, path string) bool {
	pattern = strings.TrimSpace(pattern)
	if pattern == "" {
		return false
	}
	anchored := strings.HasSuffix(pattern, "$")
	pattern = strings.TrimSuffix(pattern, "$")
	if !strings.Contains(pattern, "*") {
		if anchored {
			return path == pattern
		}
		return strings.HasPrefix(path, pattern)
	}
	parts := strings.Split(pattern, "*")
	position := 0
	for i, part := range parts {
		if part == "" {
			continue
		}
		index := strings.Index(path[position:], part)
		if index < 0 || (i == 0 && !strings.HasPrefix(path, part)) {
			return false
		}
		position += index + len(part)
	}
	if anchored {
		last := parts[len(parts)-1]
		return last == "" || strings.HasSuffix(path, last)
	}
	return true
}

type Service struct {
	pool      *pgxpool.Pool
	client    *http.Client
	userAgent string
	ttl       time.Duration
}

func NewService(pool *pgxpool.Pool, client *http.Client, userAgent string) *Service {
	return &Service{pool: pool, client: client, userAgent: userAgent, ttl: 24 * time.Hour}
}

func (s *Service) Rules(ctx context.Context, rawURL string) (Rules, error) {
	seed, err := url.Parse(rawURL)
	if err != nil {
		return Rules{}, err
	}
	domain := strings.ToLower(seed.Hostname())
	origin := &url.URL{Scheme: seed.Scheme, Host: seed.Host}

	var cached string
	var expires time.Time
	err = s.pool.QueryRow(ctx, `select rules, expires_at from fetcher.robots_cache where domain = $1`, domain).
		Scan(&cached, &expires)
	if err == nil && time.Now().Before(expires) {
		return parseString(cached, s.userAgent), nil
	}

	rules, fetchErr := s.fetch(ctx, origin.String()+"/robots.txt")
	ttl := s.ttl
	if fetchErr != nil {
		rules = Rules{Disallow: []string{"/"}}
		ttl = 5 * time.Minute
	}
	body := format(rules)
	_, _ = s.pool.Exec(ctx, `insert into fetcher.robots_cache (domain, rules, fetched_at, expires_at)
		values ($1, $2, now(), now() + ($3 * interval '1 second'))
		on conflict (domain) do update set rules = excluded.rules,
			fetched_at = excluded.fetched_at, expires_at = excluded.expires_at`,
		domain, body, ttl.Seconds())
	return rules, nil
}

func (s *Service) fetch(ctx context.Context, rawURL string) (Rules, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, rawURL, nil)
	if err != nil {
		return Rules{}, err
	}
	req.Header.Set("User-Agent", s.userAgent)
	resp, err := s.client.Do(req)
	if err != nil {
		return Rules{}, err
	}
	defer func() { _ = resp.Body.Close() }()
	switch {
	case resp.StatusCode == http.StatusNotFound || resp.StatusCode == http.StatusGone:
		return Rules{}, nil
	case resp.StatusCode < 200 || resp.StatusCode >= 300:
		return Rules{}, fmt.Errorf("robots returned %d", resp.StatusCode)
	}
	body, err := io.ReadAll(io.LimitReader(resp.Body, 256<<10))
	if err != nil {
		return Rules{}, err
	}
	return parse(string(body), s.userAgent), nil
}

func parse(body, userAgent string) Rules {
	userAgent = strings.ToLower(userAgent)
	type group struct {
		agents []string
		rules  Rules
	}
	var groups []group
	var current group
	inRules := false
	flush := func() {
		if len(current.agents) > 0 {
			groups = append(groups, current)
		}
		current = group{}
		inRules = false
	}
	for _, rawLine := range strings.Split(body, "\n") {
		line := strings.TrimSpace(strings.SplitN(rawLine, "#", 2)[0])
		if line == "" {
			continue
		}
		key, value, ok := strings.Cut(line, ":")
		if !ok {
			continue
		}
		key = strings.ToLower(strings.TrimSpace(key))
		value = strings.TrimSpace(value)
		if key == "user-agent" {
			if inRules {
				flush()
			}
			current.agents = append(current.agents, strings.ToLower(value))
			continue
		}
		inRules = true
		switch key {
		case "disallow":
			if value != "" {
				current.rules.Disallow = append(current.rules.Disallow, value)
			}
		case "allow":
			if value != "" {
				current.rules.Allow = append(current.rules.Allow, value)
			}
		case "crawl-delay":
			if seconds, err := strconv.ParseFloat(value, 64); err == nil && seconds > 0 {
				current.rules.CrawlDelay = time.Duration(seconds * float64(time.Second))
			}
		case "sitemap":
			if value != "" {
				current.rules.Sitemaps = append(current.rules.Sitemaps, value)
			}
		}
	}
	flush()

	var merged Rules
	for _, group := range groups {
		matchesGroup := false
		for _, agent := range group.agents {
			if agent == "*" || strings.Contains(userAgent, agent) {
				matchesGroup = true
				break
			}
		}
		if !matchesGroup {
			continue
		}
		merged.Disallow = append(merged.Disallow, group.rules.Disallow...)
		merged.Allow = append(merged.Allow, group.rules.Allow...)
		merged.Sitemaps = append(merged.Sitemaps, group.rules.Sitemaps...)
		if group.rules.CrawlDelay > merged.CrawlDelay {
			merged.CrawlDelay = group.rules.CrawlDelay
		}
	}
	return merged
}

func parseString(body, userAgent string) Rules { return parse(body, userAgent) }

func format(rules Rules) string {
	var b strings.Builder
	b.WriteString("User-agent: *\n")
	for _, path := range rules.Disallow {
		fmt.Fprintf(&b, "Disallow: %s\n", path)
	}
	for _, path := range rules.Allow {
		fmt.Fprintf(&b, "Allow: %s\n", path)
	}
	if rules.CrawlDelay > 0 {
		fmt.Fprintf(&b, "Crawl-delay: %.3f\n", rules.CrawlDelay.Seconds())
	}
	for _, sitemap := range rules.Sitemaps {
		fmt.Fprintf(&b, "Sitemap: %s\n", sitemap)
	}
	return b.String()
}
