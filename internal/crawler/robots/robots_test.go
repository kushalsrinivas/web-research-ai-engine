package robots

import (
	"testing"
	"time"
)

const sample = `
User-agent: *
Disallow: /private
Allow: /private/public
Crawl-delay: 2

User-agent: wre-crawler
Disallow: /blocked
`

func TestParseAppliesAgentGroups(t *testing.T) {
	rules := parse(sample, "wre-crawler/0.1")
	if rules.Allowed("/public") != true {
		t.Fatal("public path should be allowed")
	}
	if rules.Allowed("/blocked") {
		t.Fatal("agent-specific block ignored")
	}
	if rules.Allowed("/private") {
		t.Fatal("wildcard block ignored")
	}
	if !rules.Allowed("/private/public") {
		t.Fatal("longer allow must win")
	}
	if rules.CrawlDelay != 2*time.Second {
		t.Fatalf("crawl delay = %s", rules.CrawlDelay)
	}
}

func TestEmptyRobotsAllowsEverything(t *testing.T) {
	rules := parse("", "wre")
	if !rules.Allowed("/anything") {
		t.Fatal("empty rules must allow")
	}
}

func TestWildcards(t *testing.T) {
	rules := parse("User-agent: *\nDisallow: /*.pdf$\n", "wre")
	if rules.Allowed("/doc.pdf") {
		t.Fatal("pdf should be disallowed")
	}
	if !rules.Allowed("/doc.pdf.html") {
		t.Fatal("$ anchor should not match suffix")
	}
}
