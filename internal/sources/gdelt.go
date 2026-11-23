package sources

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"
)

// TopicProvider is replaceable so search vendors can change without touching
// the discovery workflow.
type TopicProvider interface {
	Search(ctx context.Context, query string, limit int) ([]Candidate, error)
}

// GDELTProvider uses the public GDELT document API. It discovers candidate
// domains; it does not treat GDELT's ranking as authority.
type GDELTProvider struct {
	client    *http.Client
	userAgent string
	endpoint  string
}

func NewGDELTProvider(client *http.Client, userAgent string) *GDELTProvider {
	return &GDELTProvider{
		client:    client,
		userAgent: userAgent,
		endpoint:  "https://api.gdeltproject.org/api/v2/doc/doc",
	}
}

// NewTopicProvider constructs the configured replaceable discovery provider.
func NewTopicProvider(name, userAgent string) (TopicProvider, error) {
	switch strings.ToLower(strings.TrimSpace(name)) {
	case "", "none", "disabled":
		return nil, nil
	case "gdelt":
		return NewGDELTProvider(newHTTPClient(30*time.Second, false), userAgent), nil
	default:
		return nil, fmt.Errorf("unsupported DISCOVERY_PROVIDER %q", name)
	}
}

func (g *GDELTProvider) Search(ctx context.Context, query string, limit int) ([]Candidate, error) {
	if limit <= 0 || limit > 100 {
		limit = 50
	}
	values := url.Values{}
	values.Set("query", query)
	values.Set("mode", "artlist")
	values.Set("maxrecords", fmt.Sprintf("%d", limit))
	values.Set("format", "json")
	values.Set("sort", "hybridrel")

	req, err := http.NewRequestWithContext(ctx, http.MethodGet, g.endpoint+"?"+values.Encode(), nil)
	if err != nil {
		return nil, err
	}
	if g.userAgent != "" {
		req.Header.Set("User-Agent", g.userAgent)
	}
	resp, err := g.client.Do(req)
	if err != nil {
		return nil, err
	}
	defer func() { _ = resp.Body.Close() }()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, 4<<10))
		return nil, fmt.Errorf("discovery provider returned %d", resp.StatusCode)
	}
	body, err := io.ReadAll(io.LimitReader(resp.Body, maxDiscoveryBody))
	if err != nil {
		return nil, err
	}
	var payload struct {
		Articles []struct {
			URL      string `json:"url"`
			Title    string `json:"title"`
			Domain   string `json:"domain"`
			Language string `json:"language"`
			SeenDate string `json:"seendate"`
		} `json:"articles"`
	}
	if err := json.Unmarshal(body, &payload); err != nil {
		return nil, fmt.Errorf("decode discovery provider response: %w", err)
	}

	candidates := make([]Candidate, 0, len(payload.Articles))
	for _, article := range payload.Articles {
		domain := strings.ToLower(strings.TrimSpace(article.Domain))
		if domain == "" {
			continue
		}
		parsed, err := url.Parse(article.URL)
		if err == nil && parsed.Hostname() != "" {
			domain = strings.ToLower(parsed.Hostname())
		}
		baseURL := "https://" + domain + "/"
		normalized, registrable, err := NormalizeBaseURL(baseURL)
		if err != nil {
			continue
		}
		candidates = append(candidates, Candidate{
			Name:       domain,
			BaseURL:    normalized,
			SourceType: TypeWebsite,
			Domain:     registrable,
			Language:   article.Language,
			Score:      0.45,
			Evidence: map[string]any{
				"provider": "gdelt",
				"query":    query,
				"article":  article.URL,
				"title":    article.Title,
				"seen_at":  article.SeenDate,
			},
			Metadata: map[string]any{"discovery": "topic"},
		})
	}
	return candidates, nil
}
