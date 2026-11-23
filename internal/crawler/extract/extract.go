// Package extract pulls follow-up URLs from HTML, sitemaps, and feeds.
package extract

import (
	"bytes"
	"net/url"
	"strings"

	"golang.org/x/net/html"
)

const DefaultMaxLinks = 500

// Links returns absolute http(s) URLs found in the document. Relative links
// are resolved against base; fragments are dropped.
func Links(body []byte, base *url.URL, max int) []string {
	if base == nil {
		return nil
	}
	if max <= 0 {
		max = DefaultMaxLinks
	}
	z := html.NewTokenizer(bytes.NewReader(body))
	seen := map[string]struct{}{}
	links := make([]string, 0, 32)
	inTextLink := false
	inLoc := false

	add := func(raw string) {
		if len(links) >= max {
			return
		}
		raw = strings.TrimSpace(raw)
		if raw == "" || strings.HasPrefix(raw, "#") {
			return
		}
		ref, err := url.Parse(raw)
		if err != nil {
			return
		}
		absolute := base.ResolveReference(ref)
		if absolute.Scheme != "http" && absolute.Scheme != "https" {
			return
		}
		absolute.Fragment = ""
		normalized := absolute.String()
		if _, ok := seen[normalized]; ok {
			return
		}
		seen[normalized] = struct{}{}
		links = append(links, normalized)
	}

	for {
		token := z.Next()
		switch token {
		case html.ErrorToken:
			return links
		case html.StartTagToken, html.SelfClosingTagToken:
			name, _ := z.TagName()
			switch string(name) {
			case "a", "area":
				if href, ok := attr(z, "href"); ok {
					add(href)
				}
			case "link":
				if href, ok := attr(z, "href"); ok {
					add(href)
				}
				inTextLink = token == html.StartTagToken
			case "loc":
				inLoc = token == html.StartTagToken
			}
		case html.EndTagToken:
			name, _ := z.TagName()
			switch string(name) {
			case "link":
				inTextLink = false
			case "loc":
				inLoc = false
			}
		case html.TextToken:
			if inTextLink || inLoc {
				add(string(z.Text()))
			}
		}
	}
}

func attr(z *html.Tokenizer, key string) (string, bool) {
	for {
		name, value, more := z.TagAttr()
		if string(name) == key {
			return string(value), true
		}
		if !more {
			return "", false
		}
	}
}
