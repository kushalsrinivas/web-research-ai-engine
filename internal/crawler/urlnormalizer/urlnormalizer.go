// Package urlnormalizer canonicalizes URLs before dedupe and fetch.
package urlnormalizer

import (
	"crypto/sha256"
	"fmt"
	"net"
	"net/url"
	"path"
	"sort"
	"strings"
)

var trackingParams = map[string]struct{}{
	"fbclid": {}, "gclid": {}, "dclid": {}, "mc_cid": {}, "mc_eid": {},
	"igshid": {}, "mkt_tok": {}, "_hsenc": {}, "_hsmi": {}, "vero_id": {},
	"vero_conv": {}, "oly_anon_id": {}, "oly_enc_id": {}, "s_cid": {},
	"ref_src": {}, "ref_url": {}, "trk": {}, "trkcampaign": {},
}

type Normalized struct {
	URL  string
	Host string
	Hash [32]byte
}

// Normalize removes fragments, default ports, tracking parameters, and
// trailing slashes while preserving query parameters that may identify a
// document.
func Normalize(raw string) (Normalized, error) {
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return Normalized{}, fmt.Errorf("url is empty")
	}
	u, err := url.Parse(raw)
	if err != nil {
		return Normalized{}, fmt.Errorf("parse url: %w", err)
	}
	if u.Scheme != "http" && u.Scheme != "https" {
		return Normalized{}, fmt.Errorf("unsupported scheme %q", u.Scheme)
	}
	if u.User != nil {
		return Normalized{}, fmt.Errorf("url must not contain userinfo")
	}
	host := strings.ToLower(u.Hostname())
	if host == "" {
		return Normalized{}, fmt.Errorf("url must include a host")
	}
	u.Scheme = strings.ToLower(u.Scheme)
	port := u.Port()
	if port == "80" && u.Scheme == "http" || port == "443" && u.Scheme == "https" {
		port = ""
	}
	if port != "" {
		u.Host = net.JoinHostPort(host, port)
	} else if strings.Contains(host, ":") {
		u.Host = "[" + host + "]"
	} else {
		u.Host = host
	}
	if u.Path == "" {
		u.Path = "/"
	}
	// shortcut: cleaning the decoded path can normalize encoded slashes (%2F).
	// Upgrade if a source uses them as document identity.
	u.Path = path.Clean(u.Path)
	if u.Path != "/" {
		u.Path = strings.TrimSuffix(u.Path, "/")
	}
	u.Fragment = ""
	u.RawFragment = ""

	query := u.Query()
	for key := range query {
		lower := strings.ToLower(key)
		if _, tracked := trackingParams[lower]; tracked || strings.HasPrefix(lower, "utm_") {
			query.Del(key)
		}
	}
	u.RawQuery = encodeSorted(query)

	normalized := u.String()
	sum := sha256.Sum256([]byte(normalized))
	return Normalized{URL: normalized, Host: host, Hash: sum}, nil
}

func encodeSorted(values url.Values) string {
	keys := make([]string, 0, len(values))
	for key := range values {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	var b strings.Builder
	for _, key := range keys {
		items := append([]string(nil), values[key]...)
		sort.Strings(items)
		for _, value := range items {
			if b.Len() > 0 {
				b.WriteByte('&')
			}
			b.WriteString(url.QueryEscape(key))
			b.WriteByte('=')
			b.WriteString(url.QueryEscape(value))
		}
	}
	return b.String()
}

func SameSite(a, b string) bool {
	hostA, errA := url.Parse(a)
	hostB, errB := url.Parse(b)
	if errA != nil || errB != nil {
		return false
	}
	return strings.EqualFold(hostA.Hostname(), hostB.Hostname())
}
