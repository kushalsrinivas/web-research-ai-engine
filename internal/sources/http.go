package sources

import (
	"context"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/netip"
	"net/url"
	"strings"
	"time"
)

var blockedPrefixes = []netip.Prefix{
	netip.MustParsePrefix("100.64.0.0/10"), // carrier-grade NAT
	netip.MustParsePrefix("192.0.0.0/24"),  // IETF protocol assignments
	netip.MustParsePrefix("198.18.0.0/15"), // benchmarking
}

// newHTTPClient returns a client that resolves and dials public addresses only,
// revalidates redirects, and bounds response time. allowPrivate exists for
// tests and must not be enabled in production.
func newHTTPClient(timeout time.Duration, allowPrivate bool) *http.Client {
	dialer := &net.Dialer{Timeout: 5 * time.Second, KeepAlive: 30 * time.Second}
	transport := &http.Transport{
		Proxy:               nil,
		MaxIdleConns:        20,
		IdleConnTimeout:     30 * time.Second,
		TLSHandshakeTimeout: 10 * time.Second,
	}
	if allowPrivate {
		transport.DialContext = dialer.DialContext
	} else {
		transport.DialContext = func(ctx context.Context, network, addr string) (net.Conn, error) {
			host, port, err := net.SplitHostPort(addr)
			if err != nil {
				return nil, err
			}
			ips, err := net.DefaultResolver.LookupIPAddr(ctx, host)
			if err != nil {
				return nil, err
			}
			var lastErr error
			for _, candidate := range ips {
				if !isPublicIP(candidate.IP) {
					continue
				}
				conn, err := dialer.DialContext(ctx, network, net.JoinHostPort(candidate.IP.String(), port))
				if err == nil {
					return conn, nil
				}
				lastErr = err
			}
			if lastErr != nil {
				return nil, lastErr
			}
			return nil, fmt.Errorf("host %s has no public address", host)
		}
	}

	return &http.Client{
		Timeout:   timeout,
		Transport: transport,
		CheckRedirect: func(req *http.Request, via []*http.Request) error {
			if len(via) >= 5 {
				return fmt.Errorf("too many redirects")
			}
			return validateFetchURL(req.URL.String())
		},
	}
}

func validateFetchURL(raw string) error {
	u, err := url.Parse(raw)
	if err != nil {
		return fmt.Errorf("invalid URL: %w", err)
	}
	if u.Scheme != "http" && u.Scheme != "https" {
		return fmt.Errorf("URL scheme must be http or https")
	}
	if u.User != nil {
		return fmt.Errorf("URL must not contain userinfo")
	}
	if u.Hostname() == "" {
		return fmt.Errorf("URL must include a host")
	}
	if u.Port() == "0" {
		return fmt.Errorf("URL port is invalid")
	}
	return nil
}

func isPublicIP(ip net.IP) bool {
	addr, ok := netip.AddrFromSlice(ip)
	if !ok {
		return false
	}
	addr = addr.Unmap()
	if !addr.IsValid() || addr.IsLoopback() || addr.IsPrivate() || addr.IsLinkLocalUnicast() ||
		addr.IsLinkLocalMulticast() || addr.IsMulticast() || addr.IsUnspecified() {
		return false
	}
	if addr.Is4() && addr.As4() == [4]byte{255, 255, 255, 255} {
		return false
	}
	for _, prefix := range blockedPrefixes {
		if prefix.Contains(addr) {
			return false
		}
	}
	return true
}

type fetchedPage struct {
	Body        []byte
	ContentType string
	FinalURL    string
	StatusCode  int
}

const maxDiscoveryBody = 2 << 20

func fetchPage(ctx context.Context, client *http.Client, rawURL, userAgent string) (fetchedPage, error) {
	if err := validateFetchURL(rawURL); err != nil {
		return fetchedPage{}, err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, rawURL, nil)
	if err != nil {
		return fetchedPage{}, err
	}
	if userAgent != "" {
		req.Header.Set("User-Agent", userAgent)
	}
	resp, err := client.Do(req)
	if err != nil {
		return fetchedPage{}, err
	}
	defer func() { _ = resp.Body.Close() }()
	body, err := io.ReadAll(io.LimitReader(resp.Body, maxDiscoveryBody+1))
	if err != nil {
		return fetchedPage{}, err
	}
	if len(body) > maxDiscoveryBody {
		return fetchedPage{}, fmt.Errorf("response exceeds discovery body limit")
	}
	return fetchedPage{
		Body:        body,
		ContentType: strings.ToLower(resp.Header.Get("Content-Type")),
		FinalURL:    resp.Request.URL.String(),
		StatusCode:  resp.StatusCode,
	}, nil
}
