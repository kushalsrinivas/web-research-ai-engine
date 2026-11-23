package urlnormalizer

import (
	"strings"
	"testing"
)

func TestNormalizeStripsNoisePreservesIdentity(t *testing.T) {
	cases := []struct {
		raw  string
		want string
	}{
		{"HTTPS://Example.COM:443/news/a/?utm_source=x&id=42#top", "https://example.com/news/a?id=42"},
		{"http://example.com:80", "http://example.com/"},
		{"http://example.com/a//b/../c/", "http://example.com/a/c"},
		{"http://example.com/page?gclid=1&fbclid=2", "http://example.com/page"},
		{"http://example.com/page?q=b&a=2", "http://example.com/page?a=2&q=b"},
	}
	for _, tc := range cases {
		got, err := Normalize(tc.raw)
		if err != nil {
			t.Fatalf("Normalize(%q): %v", tc.raw, err)
		}
		if got.URL != tc.want {
			t.Fatalf("Normalize(%q) = %q, want %q", tc.raw, got.URL, tc.want)
		}
	}
}

func TestNormalizeRejectsUnsafeInput(t *testing.T) {
	for _, raw := range []string{"", "ftp://example.com/x", "https://user:pass@example.com/", "not a url"} {
		if _, err := Normalize(raw); err == nil {
			t.Fatalf("Normalize(%q) should fail", raw)
		}
	}
}

func TestNormalizeHashIsStable(t *testing.T) {
	first, err := Normalize("https://example.com/a?utm_medium=x")
	if err != nil {
		t.Fatal(err)
	}
	second, err := Normalize("https://example.com/a/")
	if err != nil {
		t.Fatal(err)
	}
	if first.Hash != second.Hash {
		t.Fatal("equivalent URLs must hash the same")
	}
	if !strings.HasPrefix(first.URL, "https://example.com/a") {
		t.Fatalf("unexpected url %q", first.URL)
	}
}
