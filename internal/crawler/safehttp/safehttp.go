// Package safehttp builds the crawler's egress client. It resolves and dials
// public addresses directly so a second DNS answer cannot redirect the socket
// to a private range (DNS rebinding).
package safehttp

import (
	"context"
	"fmt"
	"net"
	"net/http"
	"net/netip"
	"net/url"
	"strings"
	"time"
)

var blockedPrefixes = []netip.Prefix{
	netip.MustParsePrefix("100.64.0.0/10"),
	netip.MustParsePrefix("192.0.0.0/24"),
	netip.MustParsePrefix("198.18.0.0/15"),
}

func NewClient(timeout time.Duration, allowPrivate bool) *http.Client {
	dialer := &net.Dialer{Timeout: 10 * time.Second, KeepAlive: 30 * time.Second}
	transport := &http.Transport{
		Proxy:               nil,
		MaxIdleConns:        100,
		MaxIdleConnsPerHost: 4,
		IdleConnTimeout:     60 * time.Second,
		TLSHandshakeTimeout: 10 * time.Second,
		ForceAttemptHTTP2:   true,
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
				if !IsPublic(candidate.IP) {
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
			return ValidateURL(req.URL, allowPrivate)
		},
	}
}

func ValidateURL(u *url.URL, allowPrivate bool) error {
	if u.Scheme != "http" && u.Scheme != "https" {
		return fmt.Errorf("unsupported scheme %q", u.Scheme)
	}
	if u.User != nil {
		return fmt.Errorf("url must not contain userinfo")
	}
	host := u.Hostname()
	if host == "" {
		return fmt.Errorf("url must include a host")
	}
	if !allowPrivate {
		if port := u.Port(); port != "" && port != "80" && port != "443" {
			return fmt.Errorf("port %s is not allowed", port)
		}
		if isIPAddress(host) && !IsPublic(net.ParseIP(host)) {
			return fmt.Errorf("private or reserved address is not allowed")
		}
	}
	return nil
}

func IsPublic(ip net.IP) bool {
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

func isIPAddress(host string) bool {
	return net.ParseIP(strings.Trim(host, "[]")) != nil
}
