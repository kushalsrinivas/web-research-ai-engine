package extract

import (
	"net/url"
	"testing"
)

func TestLinksResolvesRelativeAndDeduplicates(t *testing.T) {
	base, _ := url.Parse("https://example.com/news/index.html")
	body := []byte(`<html><body>
		<a href="/a">a</a>
		<a href="https://example.com/a#frag">a again</a>
		<a href="mailto:x@example.com">mail</a>
		<a href="javascript:void(0)">js</a>
	</body></html>`)
	links := Links(body, base, 0)
	if len(links) != 1 || links[0] != "https://example.com/a" {
		t.Fatalf("links = %v", links)
	}
}

func TestLinksReadsFeedAndSitemapForms(t *testing.T) {
	base, _ := url.Parse("https://example.com/")
	feed := []byte(`<rss><channel><item><link>https://example.com/post-1</link></item></channel></rss>`)
	if got := Links(feed, base, 0); len(got) != 1 || got[0] != "https://example.com/post-1" {
		t.Fatalf("rss links = %v", got)
	}
	atom := []byte(`<feed><entry><link href="https://example.com/post-2"/></entry></feed>`)
	if got := Links(atom, base, 0); len(got) != 1 || got[0] != "https://example.com/post-2" {
		t.Fatalf("atom links = %v", got)
	}
	sitemap := []byte(`<urlset><url><loc>https://example.com/page</loc></url></urlset>`)
	if got := Links(sitemap, base, 0); len(got) != 1 || got[0] != "https://example.com/page" {
		t.Fatalf("sitemap links = %v", got)
	}
}

func TestLinksHonorsCap(t *testing.T) {
	base, _ := url.Parse("https://example.com/")
	body := []byte(`<a href="/1">1</a><a href="/2">2</a><a href="/3">3</a>`)
	if got := Links(body, base, 2); len(got) != 2 {
		t.Fatalf("links = %v", got)
	}
}
