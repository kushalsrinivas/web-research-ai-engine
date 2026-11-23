package sources

import (
	"bytes"
	"encoding/xml"
	"net/url"
	"strings"

	"golang.org/x/net/html"
)

const maxExtractedLinks = 1000

func parseHTMLPage(body []byte, base *url.URL) (feeds, sitemaps, links []string) {
	tokenizer := html.NewTokenizer(bytes.NewReader(body))
	for i := 0; i < maxExtractedLinks*4; i++ {
		tt := tokenizer.Next()
		switch tt {
		case html.ErrorToken:
			return feeds, sitemaps, links
		case html.StartTagToken:
			token := tokenizer.Token()
			attrs := map[string]string{}
			for _, attr := range token.Attr {
				attrs[strings.ToLower(attr.Key)] = attr.Val
			}
			switch strings.ToLower(token.Data) {
			case "link":
				rel := strings.ToLower(attrs["rel"])
				typ := strings.ToLower(attrs["type"])
				href := resolveURL(base, attrs["href"])
				if href == "" {
					continue
				}
				if strings.Contains(rel, "sitemap") {
					sitemaps = append(sitemaps, href)
				}
				if strings.Contains(rel, "alternate") &&
					(strings.Contains(typ, "rss") || strings.Contains(typ, "atom") || strings.Contains(typ, "xml")) {
					feeds = append(feeds, href)
				}
			case "a":
				if href := resolveURL(base, attrs["href"]); href != "" && len(links) < maxExtractedLinks {
					links = append(links, href)
				}
			}
		}
	}
	return feeds, sitemaps, links
}

func parseFeedLinks(body []byte) []string {
	type rssItem struct {
		Link string `xml:"link"`
	}
	type rssDoc struct {
		Channel struct {
			Items []rssItem `xml:"item"`
		} `xml:"channel"`
		Items []rssItem `xml:"item"`
	}
	type atomLink struct {
		Href string `xml:"href,attr"`
		Rel  string `xml:"rel,attr"`
	}
	type atomEntry struct {
		Links []atomLink `xml:"link"`
	}
	type atomDoc struct {
		Entries []atomEntry `xml:"entry"`
	}

	trimmed := bytes.TrimSpace(body)
	links := make([]string, 0)
	if bytes.HasPrefix(trimmed, []byte("<rss")) || bytes.Contains(trimmed[:min(len(trimmed), 512)], []byte("<rss")) {
		var doc rssDoc
		if err := xml.Unmarshal(trimmed, &doc); err == nil {
			for _, item := range append(doc.Channel.Items, doc.Items...) {
				if link := strings.TrimSpace(item.Link); link != "" {
					links = append(links, link)
				}
			}
		}
	}
	if bytes.HasPrefix(trimmed, []byte("<feed")) || bytes.Contains(trimmed[:min(len(trimmed), 512)], []byte("<feed")) {
		var doc atomDoc
		if err := xml.Unmarshal(trimmed, &doc); err == nil {
			for _, entry := range doc.Entries {
				for _, link := range entry.Links {
					if link.Href != "" && (link.Rel == "" || link.Rel == "alternate") {
						links = append(links, link.Href)
					}
				}
			}
		}
	}
	if len(links) > maxExtractedLinks {
		links = links[:maxExtractedLinks]
	}
	return links
}

func parseSitemap(body []byte) (urls, sitemaps []string) {
	type loc struct {
		Loc string `xml:"loc"`
	}
	type urlSet struct {
		URLs []loc `xml:"url"`
	}
	type sitemapIndex struct {
		Sitemaps []loc `xml:"sitemap"`
	}
	trimmed := bytes.TrimSpace(body)
	var set urlSet
	if err := xml.Unmarshal(trimmed, &set); err == nil {
		for _, item := range set.URLs {
			if item.Loc != "" {
				urls = append(urls, strings.TrimSpace(item.Loc))
			}
		}
	}
	var index sitemapIndex
	if err := xml.Unmarshal(trimmed, &index); err == nil {
		for _, item := range index.Sitemaps {
			if item.Loc != "" {
				sitemaps = append(sitemaps, strings.TrimSpace(item.Loc))
			}
		}
	}
	if len(urls) > maxExtractedLinks {
		urls = urls[:maxExtractedLinks]
	}
	if len(sitemaps) > 10 {
		sitemaps = sitemaps[:10]
	}
	return urls, sitemaps
}

func resolveURL(base *url.URL, href string) string {
	href = strings.TrimSpace(href)
	if href == "" || strings.HasPrefix(href, "#") ||
		strings.HasPrefix(href, "mailto:") || strings.HasPrefix(href, "javascript:") ||
		strings.HasPrefix(href, "tel:") {
		return ""
	}
	ref, err := url.Parse(href)
	if err != nil {
		return ""
	}
	resolved := base.ResolveReference(ref)
	if resolved.Scheme != "http" && resolved.Scheme != "https" {
		return ""
	}
	resolved.Fragment = ""
	return resolved.String()
}

func isFeedContent(contentType string, body []byte) bool {
	trimmed := bytes.TrimSpace(body)
	head := trimmed[:min(len(trimmed), 1024)]
	if bytes.Contains(head, []byte("<rss")) || bytes.Contains(head, []byte("<feed")) {
		return true
	}
	contentType = strings.ToLower(contentType)
	return strings.Contains(contentType, "rss") || strings.Contains(contentType, "atom")
}

func isSitemapContent(body []byte) bool {
	trimmed := bytes.TrimSpace(body)
	return bytes.Contains(trimmed[:min(len(trimmed), 1024)], []byte("<urlset")) ||
		bytes.Contains(trimmed[:min(len(trimmed), 1024)], []byte("<sitemapindex"))
}
