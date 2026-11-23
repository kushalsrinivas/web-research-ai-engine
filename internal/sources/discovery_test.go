package sources

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestDiscoverSeedFindsFeedSiteAndSitemap(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		fmt.Fprint(w, `<html><head><link rel="alternate" type="application/rss+xml" href="/feed.xml"></head>
			<body><a href="/article">article</a></body></html>`)
	})
	mux.HandleFunc("/robots.txt", func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprint(w, "User-agent: *\nDisallow: /private\n", "Sitemap: "+localURL(r)+"/sitemap.xml\n")
	})
	mux.HandleFunc("/sitemap.xml", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/xml")
		fmt.Fprintf(w, `<urlset><url><loc>%s/article</loc></url></urlset>`, localURL(r))
	})
	mux.HandleFunc("/feed.xml", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/rss+xml")
		fmt.Fprintf(w, `<rss><channel><item><link>%s/article</link></item></channel></rss>`, localURL(r))
	})
	server := httptest.NewServer(mux)
	defer server.Close()

	discovery := NewDiscovery(nil, "test-agent", true)
	candidates, err := discovery.DiscoverSeed(context.Background(), baseURL(server.URL), 10)
	if err != nil {
		t.Fatal(err)
	}
	if len(candidates) == 0 {
		t.Fatal("expected candidates")
	}
	var sawFeed, sawSite bool
	for _, candidate := range candidates {
		if candidate.SourceType == TypeRSS && strings.Contains(candidate.BaseURL, "/feed.xml") {
			sawFeed = true
		}
		if candidate.SourceType == TypeWebsite && strings.HasSuffix(candidate.BaseURL, "/") {
			sawSite = true
		}
	}
	if !sawFeed || !sawSite {
		t.Fatalf("missing candidates: %+v", candidates)
	}
}

func TestDiscoverSeedHonorsRobots(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/robots.txt" {
			fmt.Fprint(w, "User-agent: *\nDisallow: /private\n")
			return
		}
		fmt.Fprint(w, "should not be fetched")
	}))
	defer server.Close()
	discovery := NewDiscovery(nil, "test-agent", true)
	_, err := discovery.DiscoverSeed(context.Background(), baseURL(server.URL)+"/private", 5)
	if !errors.Is(err, ErrRobotsDenied) {
		t.Fatalf("error = %v, want ErrRobotsDenied", err)
	}
}

type fakeTopicProvider struct {
	candidates []Candidate
}

func (f *fakeTopicProvider) Search(context.Context, string, int) ([]Candidate, error) {
	return f.candidates, nil
}

func TestDiscoverTopicMergesQueries(t *testing.T) {
	provider := &fakeTopicProvider{candidates: []Candidate{{
		Name: "example", BaseURL: "https://example.com/", SourceType: TypeWebsite,
		Domain: "example.com", Score: 0.5,
	}}}
	discovery := NewDiscovery(provider, "test-agent", true)
	candidates, err := discovery.DiscoverTopic(context.Background(), "semiconductors", []string{"query one", "query two"}, 5)
	if err != nil {
		t.Fatal(err)
	}
	if len(candidates) != 1 {
		t.Fatalf("candidates = %d, want 1", len(candidates))
	}
	queries, _ := candidates[0].Evidence["queries"].([]string)
	if len(queries) != 2 {
		t.Fatalf("queries = %#v", candidates[0].Evidence["queries"])
	}
}

func TestDiscoverGraphFindsExternalDomain(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/html")
		fmt.Fprint(w, `<html><body><a href="https://external.example/a">a</a><a href="https://external.example/b">b</a></body></html>`)
	})
	mux.HandleFunc("/robots.txt", func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprint(w, "User-agent: *\nAllow: /\n")
	})
	server := httptest.NewServer(mux)
	defer server.Close()

	discovery := NewDiscovery(nil, "test-agent", true)
	source := Source{
		BaseURL: baseURL(server.URL), Domain: "localhost", RobotsPolicy: RobotsRespect,
	}
	candidates, err := discovery.DiscoverGraph(context.Background(), source, 5)
	if err != nil {
		t.Fatal(err)
	}
	if len(candidates) != 1 || candidates[0].Domain != "external.example" {
		t.Fatalf("candidates = %+v", candidates)
	}
}

func TestGDELTProviderParsesCandidates(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		fmt.Fprint(w, `{"articles":[{"url":"https://example.com/a","title":"A","domain":"example.com","language":"English","seendate":"20261123100000"}]}`)
	}))
	defer server.Close()
	provider := NewGDELTProvider(server.Client(), "test-agent")
	provider.endpoint = server.URL
	candidates, err := provider.Search(context.Background(), "semiconductors", 10)
	if err != nil {
		t.Fatal(err)
	}
	if len(candidates) != 1 || candidates[0].Domain != "example.com" {
		t.Fatalf("candidates = %+v", candidates)
	}
}

func localURL(r *http.Request) string {
	return baseURL("http://" + r.Host)
}

func baseURL(raw string) string {
	return strings.Replace(raw, "127.0.0.1", "localhost", 1)
}
