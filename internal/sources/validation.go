package sources

import (
	"fmt"
	"net"
	"net/url"
	"path"
	"strings"

	"golang.org/x/net/publicsuffix"
)

const (
	maxNameLength     = 200
	maxURLLength      = 2048
	maxLanguageLength = 35
)

// NormalizeBaseURL canonicalizes a source URL and returns its registrable
// domain. Fragments and default ports are removed; query parameters are sorted.
func NormalizeBaseURL(raw string) (string, string, error) {
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return "", "", fmt.Errorf("base_url is required")
	}
	if len(raw) > maxURLLength {
		return "", "", fmt.Errorf("base_url is too long")
	}
	u, err := url.Parse(raw)
	if err != nil {
		return "", "", fmt.Errorf("base_url is invalid: %w", err)
	}
	if u.Scheme != "http" && u.Scheme != "https" {
		return "", "", fmt.Errorf("base_url must use http or https")
	}
	if u.User != nil {
		return "", "", fmt.Errorf("base_url must not contain userinfo")
	}
	host := strings.ToLower(u.Hostname())
	if host == "" {
		return "", "", fmt.Errorf("base_url must include a host")
	}
	if port := u.Port(); port != "" && port != "80" && port != "443" {
		// Non-default ports are valid for public sources, but never credentials.
		if port == "0" {
			return "", "", fmt.Errorf("base_url port is invalid")
		}
	}
	u.Scheme = strings.ToLower(u.Scheme)
	hostPort := host
	if port := u.Port(); port != "" {
		hostPort = net.JoinHostPort(host, port)
	} else if strings.Contains(host, ":") {
		hostPort = "[" + host + "]"
	}
	u.Host = hostPort
	if port := u.Port(); port == "80" && u.Scheme == "http" || port == "443" && u.Scheme == "https" {
		if strings.Contains(host, ":") {
			u.Host = "[" + host + "]"
		} else {
			u.Host = host
		}
	}
	if u.Path == "" {
		u.Path = "/"
	}
	u.Path = path.Clean(u.Path)
	if u.Path != "/" {
		u.Path = strings.TrimSuffix(u.Path, "/")
	}
	u.RawQuery = u.Query().Encode()
	u.Fragment = ""
	u.RawFragment = ""

	domain, err := RegistrableDomain(host)
	if err != nil {
		return "", "", err
	}
	return u.String(), domain, nil
}

// RegistrableDomain returns the public-suffix-aware domain, falling back to the
// host for localhost, IPs, and test hosts.
func RegistrableDomain(host string) (string, error) {
	host = strings.TrimSuffix(strings.ToLower(host), ".")
	if host == "" {
		return "", fmt.Errorf("host is empty")
	}
	if strings.Contains(host, ":") {
		// IPv6 literal.
		return host, nil
	}
	if isIPAddress(host) || host == "localhost" || !strings.Contains(host, ".") {
		return host, nil
	}
	domain, err := publicsuffix.EffectiveTLDPlusOne(host)
	if err != nil {
		return host, nil
	}
	return strings.ToLower(domain), nil
}

func isIPAddress(host string) bool {
	return net.ParseIP(host) != nil
}

// ValidateSource applies defaults and validates all source invariants in place.
func ValidateSource(s *Source) error {
	s.Name = strings.TrimSpace(s.Name)
	if s.Name == "" {
		s.Name = defaultSourceName(s.BaseURL)
	}
	if len(s.Name) > maxNameLength {
		return fmt.Errorf("name is too long")
	}
	if len(s.Language) > maxLanguageLength {
		return fmt.Errorf("language is too long")
	}
	if s.Priority == 0 {
		s.Priority = 5
	}
	if s.Priority < 1 || s.Priority > 10 {
		return fmt.Errorf("priority must be between 1 and 10")
	}
	if s.RobotsPolicy == "" {
		s.RobotsPolicy = RobotsRespect
	}
	if _, ok := allowedRobotsPolicies[s.RobotsPolicy]; !ok {
		return fmt.Errorf("robots_policy must be respect, allow or deny")
	}
	if s.Status == "" {
		s.Status = StatusActive
	}
	if _, ok := allowedStatuses[s.Status]; !ok {
		return fmt.Errorf("status must be active, candidate, disabled or rejected")
	}
	if _, ok := allowedSourceTypes[s.SourceType]; !ok {
		return fmt.Errorf("source_type is not supported")
	}
	if s.Status == StatusCandidate || s.Status == StatusDisabled || s.Status == StatusRejected {
		s.CrawlEnabled = false
	}
	if s.CrawlFrequencySeconds != nil && *s.CrawlFrequencySeconds < 60 {
		return fmt.Errorf("crawl_frequency_seconds must be at least 60")
	}
	if s.Metadata == nil {
		s.Metadata = map[string]any{}
	}

	normalized, domain, err := NormalizeBaseURL(s.BaseURL)
	if err != nil {
		return err
	}
	s.BaseURL = normalized
	s.Domain = domain
	return nil
}

func defaultSourceName(baseURL string) string {
	u, err := url.Parse(baseURL)
	if err != nil {
		return "unnamed source"
	}
	if u.Hostname() == "" {
		return "unnamed source"
	}
	return u.Hostname()
}

// ValidateDomainPolicy applies defaults and validates policy invariants.
func ValidateDomainPolicy(p *DomainPolicy) error {
	p.Domain = strings.TrimSuffix(strings.ToLower(strings.TrimSpace(p.Domain)), ".")
	if p.Domain == "" {
		return fmt.Errorf("domain is required")
	}
	if p.MaxConcurrency == 0 {
		p.MaxConcurrency = 1
	}
	if p.RequestsPerMinute == 0 {
		p.RequestsPerMinute = 30
	}
	if p.CrawlDelayMS == 0 {
		p.CrawlDelayMS = 1000
	}
	if p.RobotsPolicy == "" {
		p.RobotsPolicy = RobotsRespect
	}
	if p.MaxConcurrency < 1 || p.MaxConcurrency > 100 {
		return fmt.Errorf("max_concurrency must be between 1 and 100")
	}
	if p.RequestsPerMinute < 1 || p.RequestsPerMinute > 10000 {
		return fmt.Errorf("requests_per_minute must be between 1 and 10000")
	}
	if p.CrawlDelayMS < 0 || p.CrawlDelayMS > 3600000 {
		return fmt.Errorf("crawl_delay_ms must be between 0 and 3600000")
	}
	if _, ok := allowedRobotsPolicies[p.RobotsPolicy]; !ok {
		return fmt.Errorf("robots_policy must be respect, allow or deny")
	}
	if p.Metadata == nil {
		p.Metadata = map[string]any{}
	}
	return nil
}

// ValidateCrawlOutcome validates a crawler report.
func ValidateCrawlOutcome(o *CrawlOutcome) error {
	switch o.Status {
	case "success", "failure", "skipped":
	default:
		return fmt.Errorf("status must be success, failure or skipped")
	}
	if o.ItemsFound < 0 {
		return fmt.Errorf("items_found must be non-negative")
	}
	if o.RetryAfterSeconds != nil && *o.RetryAfterSeconds < 0 {
		return fmt.Errorf("retry_after_seconds must be non-negative")
	}
	if o.Trigger == "" {
		o.Trigger = "scheduled"
	}
	return nil
}

// ClampLimit keeps list endpoints bounded.
func ClampLimit(limit int) int {
	if limit <= 0 {
		return 50
	}
	if limit > 200 {
		return 200
	}
	return limit
}
