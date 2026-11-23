package sources

import (
	"strings"
	"testing"
)

func TestNormalizeBaseURL(t *testing.T) {
	normalized, domain, err := NormalizeBaseURL("HTTPS://Example.COM:443/story/?b=2&a=1#frag")
	if err != nil {
		t.Fatal(err)
	}
	if normalized != "https://example.com/story?a=1&b=2" {
		t.Fatalf("normalized = %q", normalized)
	}
	if domain != "example.com" {
		t.Fatalf("domain = %q", domain)
	}

	normalized, domain, err = NormalizeBaseURL("https://news.example.co.uk/feed")
	if err != nil {
		t.Fatal(err)
	}
	if normalized != "https://news.example.co.uk/feed" || domain != "example.co.uk" {
		t.Fatalf("normalized = %q, domain = %q", normalized, domain)
	}
}

func TestNormalizeBaseURLRejectsUnsafeInputs(t *testing.T) {
	for _, raw := range []string{
		"ftp://example.com",
		"https://user:pass@example.com",
		"https:///missing-host",
		"",
	} {
		if _, _, err := NormalizeBaseURL(raw); err == nil {
			t.Fatalf("expected rejection for %q", raw)
		}
	}
}

func TestValidateSourceDefaultsAndCandidateRule(t *testing.T) {
	source := Source{
		BaseURL:    "https://example.com",
		SourceType: TypeNews,
		Status:     StatusCandidate,
	}
	if err := ValidateSource(&source); err != nil {
		t.Fatal(err)
	}
	if source.Priority != 5 || source.RobotsPolicy != RobotsRespect {
		t.Fatalf("defaults not applied: %+v", source)
	}
	if source.CrawlEnabled {
		t.Fatal("candidates must not be crawl-enabled")
	}
	if source.Name != "example.com" {
		t.Fatalf("name = %q", source.Name)
	}
}

func TestValidateSourceRejectsBadValues(t *testing.T) {
	cases := []Source{
		{BaseURL: "https://example.com", SourceType: "unknown"},
		{BaseURL: "https://example.com", SourceType: TypeNews, Priority: 11},
		{BaseURL: "https://example.com", SourceType: TypeNews, RobotsPolicy: "ignore"},
		{BaseURL: "https://example.com", SourceType: TypeNews, CrawlFrequencySeconds: intPtr(10)},
	}
	for _, source := range cases {
		if err := ValidateSource(&source); err == nil {
			t.Fatalf("expected rejection for %+v", source)
		}
	}
}

func TestValidateDomainPolicyDefaults(t *testing.T) {
	policy := DomainPolicy{Domain: "Example.COM"}
	if err := ValidateDomainPolicy(&policy); err != nil {
		t.Fatal(err)
	}
	if policy.Domain != "example.com" || policy.MaxConcurrency != 1 ||
		policy.RequestsPerMinute != 30 || policy.CrawlDelayMS != 1000 {
		t.Fatalf("defaults not applied: %+v", policy)
	}
}

func TestPublicIPClassification(t *testing.T) {
	for _, raw := range []string{"127.0.0.1", "10.0.0.1", "169.254.169.254", "100.64.0.1", "::1"} {
		if isPublicIPString(raw) {
			t.Fatalf("%s should not be public", raw)
		}
	}
	for _, raw := range []string{"8.8.8.8", "1.1.1.1", "2606:4700:4700::1111"} {
		if !isPublicIPString(raw) {
			t.Fatalf("%s should be public", raw)
		}
	}
}

func TestNormalizeBaseURLLowercasesNonDefaultPortHost(t *testing.T) {
	normalized, _, err := NormalizeBaseURL("http://Example.COM:8080/feed")
	if err != nil {
		t.Fatal(err)
	}
	if !strings.HasPrefix(normalized, "http://example.com:8080/") {
		t.Fatalf("normalized = %q", normalized)
	}
}

func intPtr(value int) *int { return &value }
