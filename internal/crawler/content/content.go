// Package content detects the document kind used for archive and parsing.
package content

import (
	"bytes"
	"mime"
	"net/http"
	"strings"
)

type Kind string

const (
	HTML  Kind = "html"
	Text  Kind = "text"
	PDF   Kind = "pdf"
	XML   Kind = "xml"
	JSON  Kind = "json"
	RSS   Kind = "rss"
	Atom  Kind = "atom"
	Image Kind = "image"
	Other Kind = "other"
)

func Detect(contentType string, body []byte) Kind {
	mediaType, _, _ := mime.ParseMediaType(contentType)
	mediaType = strings.ToLower(mediaType)
	head := bytes.TrimSpace(body)
	if len(head) > 2048 {
		head = head[:2048]
	}
	lowerHead := bytes.ToLower(head)

	switch {
	case bytes.HasPrefix(head, []byte("%PDF-")):
		return PDF
	case bytes.Contains(lowerHead, []byte("<rss")):
		return RSS
	case bytes.Contains(lowerHead, []byte("<feed")):
		return Atom
	case bytes.Contains(lowerHead, []byte("<urlset")), bytes.Contains(lowerHead, []byte("<sitemapindex")):
		return XML
	case bytes.Contains(lowerHead, []byte("<!doctype html")), bytes.Contains(lowerHead, []byte("<html")):
		return HTML
	case mediaType == "application/json", mediaType == "application/ld+json":
		return JSON
	case strings.HasPrefix(mediaType, "image/"):
		return Image
	case mediaType == "application/pdf":
		return PDF
	case strings.Contains(mediaType, "rss"):
		return RSS
	case strings.Contains(mediaType, "atom"):
		return Atom
	case mediaType == "text/html", mediaType == "application/xhtml+xml":
		return HTML
	case strings.Contains(mediaType, "xml"):
		return XML
	case strings.HasPrefix(mediaType, "text/"):
		return Text
	}

	switch detected := http.DetectContentType(head); {
	case strings.HasPrefix(detected, "text/html"):
		return HTML
	case strings.HasPrefix(detected, "application/pdf"):
		return PDF
	case strings.HasPrefix(detected, "image/"):
		return Image
	}
	return Other
}

func (k Kind) Textual() bool {
	switch k {
	case HTML, Text, XML, JSON, RSS, Atom:
		return true
	default:
		return false
	}
}

func (k Kind) LinksAllowed() bool {
	switch k {
	case HTML, XML, RSS, Atom:
		return true
	default:
		return false
	}
}

func (k Kind) Extension() string {
	switch k {
	case HTML:
		return ".html"
	case Text:
		return ".txt"
	case PDF:
		return ".pdf"
	case XML:
		return ".xml"
	case JSON:
		return ".json"
	case RSS:
		return ".rss.xml"
	case Atom:
		return ".atom.xml"
	case Image:
		return ".img"
	default:
		return ".bin"
	}
}
