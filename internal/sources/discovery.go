package sources

import (
	"context"
	"errors"
	"fmt"
	"net"
	"net/http"
	"net/url"
	"sort"
	"strings"
	"time"
)

var (
	ErrRobotsDenied        = errors.New("robots.txt disallows discovery")
	ErrProviderUnavailable = errors.New("topic discovery provider is not configured")
	ErrPolicyDenied        = errors.New("domain policy denies discovery")
	ErrInvalidRequest      = errors.New("invalid discovery request")
)

// Discovery implements seed, topic, and graph discovery. It never registers a
// candidate itself; the handler decides whether to persist candidates.
type Discovery struct {
	client    *http.Client
	provider  TopicProvider
	userAgent string
}

func NewDiscovery(provider TopicProvider, userAgent string, allowPrivate bool) *Discovery {
	return &Discovery{
		client:    newHTTPClient(20*time.Second, allowPrivate),
		provider:  provider,
		userAgent: userAgent,
	}
}

// DiscoverSeed finds feeds, sitemaps, and a website candidate for a seed URL.
func (d *Discovery) DiscoverSeed(ctx context.Context, rawURL string, max int) ([]Candidate, error) {
	max = clampCandidates(max)
	seed, err := url.Parse(rawURL)
	if err != nil {
		return nil, fmt.Errorf("%w: invalid seed URL: %v", ErrInvalidRequest, err)
	}
	if err := validateFetchURL(seed.String()); err != nil {
		return nil, fmt.Errorf("%w: %v", ErrInvalidRequest, err)
	}
	if isIPAddress(seed.Hostname()) && !isPublicIPString(seed.Hostname()) {
		return nil, fmt.Errorf("%w: seed resolves to a non-public address", ErrInvalidRequest)
	}

	robots := fetchRobots(ctx, d.client, seed, d.userAgent)
	if !robots.allowed(seed.Path) {
		return nil, ErrRobotsDenied
	}
	page, err := fetchPage(ctx, d.client, seed.String(), d.userAgent)
	if err != nil {
		return nil, err
	}
	if page.StatusCode < 200 || page.StatusCode >= 400 {
		return nil, fmt.Errorf("seed returned HTTP %d", page.StatusCode)
	}
	return d.candidatesFromPage(ctx, page, seed, max, robots)
}

func (d *Discovery) candidatesFromPage(ctx context.Context, page fetchedPage, seed *url.URL, max int, robots robotsRules) ([]Candidate, error) {
	candidates := map[string]Candidate{}
	add := func(name, raw, sourceType string, score float64, evidence map[string]any) {
		candidate, ok := normalizeCandidate(name, raw, sourceType, score, evidence)
		if !ok {
			return
		}
		if existing, exists := candidates[candidate.BaseURL]; !exists || candidate.Score > existing.Score {
			candidates[candidate.BaseURL] = candidate
		}
	}

	origin := &url.URL{Scheme: seed.Scheme, Host: seed.Host}
	if isFeedContent(page.ContentType, page.Body) {
		add(seed.Hostname(), seed.String(), TypeRSS, 0.90, map[string]any{
			"mechanism": "seed", "signal": "feed_content", "content_type": page.ContentType,
		})
	} else if isSitemapContent(page.Body) {
		add(seed.Hostname(), origin.String(), TypeWebsite, 0.80, map[string]any{
			"mechanism": "seed", "signal": "sitemap_content",
		})
	} else {
		feeds, sitemapLinks, links := parseHTMLPage(page.Body, seed)
		for _, feed := range feeds {
			add(seed.Hostname(), feed, TypeRSS, 0.90, map[string]any{
				"mechanism": "seed", "signal": "feed_autodiscovery",
			})
		}
		for _, sitemap := range sitemapLinks {
			add(seed.Hostname(), sitemap, TypeWebsite, 0.70, map[string]any{
				"mechanism": "seed", "signal": "sitemap_link",
			})
		}
		if len(links) > 0 {
			add(seed.Hostname(), origin.String(), TypeWebsite, 0.45, map[string]any{
				"mechanism": "seed", "signal": "website", "same_domain_links": len(links),
			})
			external := map[string]int{}
			for _, link := range links {
				parsed, err := url.Parse(link)
				if err != nil {
					continue
				}
				domain, err := RegistrableDomain(parsed.Hostname())
				if err != nil || domain == "" || domain == seed.Hostname() {
					continue
				}
				external[domain]++
			}
			for domain, count := range external {
				score := 0.25 + float64(count)*0.03
				if score > 0.50 {
					score = 0.50
				}
				add(domain, "https://"+domain+"/", TypeWebsite, score, map[string]any{
					"mechanism": "seed", "signal": "outbound_link", "link_count": count,
					"referrer": seed.String(),
				})
			}
		}
	}
	add(seed.Hostname(), origin.String(), TypeWebsite, 0.40, map[string]any{
		"mechanism": "seed", "signal": "website",
	})

	sitemaps := append([]string{}, robots.Sitemaps...)
	if len(sitemaps) == 0 {
		sitemaps = append(sitemaps, origin.String()+"/sitemap.xml")
	}
	for i, sitemap := range uniqueStrings(sitemaps) {
		if i >= 3 {
			break
		}
		if ref, err := url.Parse(sitemap); err == nil {
			sitemap = origin.ResolveReference(ref).String()
		}
		page, err := fetchPage(ctx, d.client, sitemap, d.userAgent)
		if err != nil || page.StatusCode < 200 || page.StatusCode >= 300 {
			add(seed.Hostname(), sitemap, TypeWebsite, 0.45, map[string]any{
				"mechanism": "seed", "signal": "sitemap_declared",
			})
			continue
		}
		if !isSitemapContent(page.Body) {
			continue
		}
		add(seed.Hostname(), origin.String(), TypeWebsite, 0.75, map[string]any{
			"mechanism": "seed", "signal": "sitemap", "sitemap": sitemap,
		})
	}

	result := make([]Candidate, 0, len(candidates))
	for _, candidate := range candidates {
		result = append(result, candidate)
	}
	sortCandidates(result)
	if len(result) > max {
		result = result[:max]
	}
	return result, nil
}

// DiscoverTopic turns configurable topic queries into candidate domains.
func (d *Discovery) DiscoverTopic(ctx context.Context, topic string, queries []string, max int) ([]Candidate, error) {
	if d.provider == nil {
		return nil, ErrProviderUnavailable
	}
	max = clampCandidates(max)
	if len(queries) == 0 {
		if strings.TrimSpace(topic) == "" {
			return nil, fmt.Errorf("%w: topic or queries are required", ErrInvalidRequest)
		}
		queries = []string{topic}
	}
	merged := map[string]Candidate{}
	for _, query := range queries {
		query = strings.TrimSpace(query)
		if query == "" {
			continue
		}
		found, err := d.provider.Search(ctx, query, max*2)
		if err != nil {
			return nil, err
		}
		for _, candidate := range found {
			candidate, ok := normalizeCandidate(candidate.Name, candidate.BaseURL, candidate.SourceType, candidate.Score, candidate.Evidence)
			if !ok {
				continue
			}
			if existing, ok := merged[candidate.BaseURL]; ok {
				if candidate.Score > existing.Score {
					existing.Score = candidate.Score
				}
				if existing.Evidence == nil {
					existing.Evidence = map[string]any{}
				}
				existing.Evidence["queries"] = appendStrings(existing.Evidence["queries"], query)
				merged[candidate.BaseURL] = existing
				continue
			}
			if candidate.Evidence == nil {
				candidate.Evidence = map[string]any{}
			}
			candidate.Evidence["queries"] = []string{query}
			merged[candidate.BaseURL] = candidate
		}
	}
	result := make([]Candidate, 0, len(merged))
	for _, candidate := range merged {
		result = append(result, candidate)
	}
	sortCandidates(result)
	if len(result) > max {
		result = result[:max]
	}
	return result, nil
}

// DiscoverGraph examines links from an existing source and proposes external
// domains. It does not recurse.
func (d *Discovery) DiscoverGraph(ctx context.Context, source Source, max int) ([]Candidate, error) {
	max = clampCandidates(max)
	seed, err := url.Parse(source.BaseURL)
	if err != nil {
		return nil, err
	}
	robots := fetchRobots(ctx, d.client, seed, d.userAgent)
	if source.RobotsPolicy == RobotsDeny ||
		(source.RobotsPolicy == RobotsRespect && !robots.allowed(seed.Path)) {
		return nil, ErrRobotsDenied
	}
	page, err := fetchPage(ctx, d.client, source.BaseURL, d.userAgent)
	if err != nil {
		return nil, err
	}
	if page.StatusCode < 200 || page.StatusCode >= 400 {
		return nil, fmt.Errorf("source returned HTTP %d", page.StatusCode)
	}

	var links []string
	switch {
	case isFeedContent(page.ContentType, page.Body):
		links = parseFeedLinks(page.Body)
	case isSitemapContent(page.Body):
		links, _ = parseSitemap(page.Body)
	default:
		_, _, links = parseHTMLPage(page.Body, seed)
	}

	counts := map[string]int{}
	example := map[string]string{}
	for _, link := range links {
		resolved, err := url.Parse(link)
		if err != nil {
			continue
		}
		domain, err := RegistrableDomain(resolved.Hostname())
		if err != nil || domain == "" || domain == source.Domain || domain == "localhost" {
			continue
		}
		if isIPAddress(domain) && !isPublicIPString(domain) {
			continue
		}
		counts[domain]++
		if _, ok := example[domain]; !ok {
			example[domain] = resolved.String()
		}
	}

	result := make([]Candidate, 0, len(counts))
	for domain, count := range counts {
		score := 0.30 + float64(count)*0.05
		if score > 0.75 {
			score = 0.75
		}
		candidate, ok := normalizeCandidate(domain, "https://"+domain+"/", TypeWebsite, score, map[string]any{
			"mechanism": "graph", "referrer": source.BaseURL, "link_count": count, "example": example[domain],
		})
		if ok {
			result = append(result, candidate)
		}
	}
	sortCandidates(result)
	if len(result) > max {
		result = result[:max]
	}
	return result, nil
}

func normalizeCandidate(name, raw, sourceType string, score float64, evidence map[string]any) (Candidate, bool) {
	baseURL, domain, err := NormalizeBaseURL(raw)
	if err != nil {
		return Candidate{}, false
	}
	parsed, err := url.Parse(baseURL)
	if err != nil {
		return Candidate{}, false
	}
	if isIPAddress(parsed.Hostname()) && !isPublicIPString(parsed.Hostname()) {
		return Candidate{}, false
	}
	if _, ok := allowedSourceTypes[sourceType]; !ok {
		sourceType = TypeWebsite
	}
	if name == "" {
		name = domain
	}
	if score < 0 {
		score = 0
	}
	if score > 1 {
		score = 1
	}
	return Candidate{
		Name:       name,
		BaseURL:    baseURL,
		SourceType: sourceType,
		Domain:     domain,
		Score:      score,
		Evidence:   evidence,
		Metadata:   map[string]any{"discovery": "candidate"},
	}, true
}

func clampCandidates(max int) int {
	if max <= 0 {
		return 10
	}
	if max > 50 {
		return 50
	}
	return max
}

func uniqueStrings(values []string) []string {
	seen := map[string]struct{}{}
	result := make([]string, 0, len(values))
	for _, value := range values {
		if _, ok := seen[value]; ok || value == "" {
			continue
		}
		seen[value] = struct{}{}
		result = append(result, value)
	}
	return result
}

func appendStrings(existing any, value string) []string {
	switch current := existing.(type) {
	case []string:
		return append(current, value)
	case string:
		return []string{current, value}
	default:
		return []string{value}
	}
}

func isPublicIPString(raw string) bool {
	ip := net.ParseIP(raw)
	return ip != nil && isPublicIP(ip)
}

func sortCandidates(candidates []Candidate) {
	sort.Slice(candidates, func(i, j int) bool {
		if candidates[i].Score == candidates[j].Score {
			return candidates[i].BaseURL < candidates[j].BaseURL
		}
		return candidates[i].Score > candidates[j].Score
	})
}
