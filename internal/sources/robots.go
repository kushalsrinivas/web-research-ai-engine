package sources

import (
	"context"
	"net/http"
	"net/url"
	"strings"
)

type robotsRules struct {
	Sitemaps   []string
	Disallow   []string
	CrawlDelay int
}

func parseRobots(body string) robotsRules {
	var rules robotsRules
	for _, line := range strings.Split(body, "\n") {
		line = strings.TrimSpace(strings.SplitN(line, "#", 2)[0])
		if line == "" {
			continue
		}
		key, value, ok := strings.Cut(line, ":")
		if !ok {
			continue
		}
		key = strings.ToLower(strings.TrimSpace(key))
		value = strings.TrimSpace(value)
		switch key {
		case "sitemap":
			if value != "" {
				rules.Sitemaps = append(rules.Sitemaps, value)
			}
		case "disallow":
			if value != "" {
				rules.Disallow = append(rules.Disallow, value)
			}
		case "crawl-delay":
			if seconds := atoi(value); seconds > 0 {
				rules.CrawlDelay = seconds
			}
		}
	}
	return rules
}

func (r robotsRules) allowed(path string) bool {
	if path == "" {
		path = "/"
	}
	for _, disallow := range r.Disallow {
		if strings.HasPrefix(path, disallow) {
			return false
		}
	}
	return true
}

func fetchRobots(ctx context.Context, client *http.Client, seed *url.URL, userAgent string) robotsRules {
	origin := &url.URL{Scheme: seed.Scheme, Host: seed.Host}
	page, err := fetchPage(ctx, client, origin.String()+"/robots.txt", userAgent)
	if err != nil {
		// Conservative RFC 9309 reading: an unavailable robots file blocks
		// discovery until a successful fetch.
		return robotsRules{Disallow: []string{"/"}}
	}
	if page.StatusCode == http.StatusNotFound || page.StatusCode == http.StatusGone {
		return robotsRules{}
	}
	if page.StatusCode < 200 || page.StatusCode >= 300 {
		return robotsRules{Disallow: []string{"/"}}
	}
	return parseRobots(string(page.Body))
}

func atoi(raw string) int {
	value := 0
	for _, r := range raw {
		if r < '0' || r > '9' {
			return 0
		}
		value = value*10 + int(r-'0')
	}
	return value
}
