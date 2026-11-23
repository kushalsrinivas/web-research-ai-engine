// Package fetcher performs bounded HTTP fetches using the crawler's egress
// policy.
package fetcher

import (
	"context"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"time"

	"github.com/kushalsrinivas/web-research-ai-engine/internal/crawler/content"
	"github.com/kushalsrinivas/web-research-ai-engine/internal/crawler/safehttp"
)

type Config struct {
	Timeout      time.Duration
	MaxBodyBytes int64
	UserAgent    string
	AllowPrivate bool
}

type CacheEntry struct {
	ETag         string
	LastModified time.Time
}

type Result struct {
	RequestedURL string
	FinalURL     string
	HTTPStatus   int
	Header       http.Header
	Body         []byte
	ContentType  string
	ContentKind  content.Kind
	NotModified  bool
	ETag         string
	LastModified time.Time
	FetchedAt    time.Time
	Duration     time.Duration
	RetryAfter   time.Duration
}

type Fetcher struct {
	client       *http.Client
	userAgent    string
	maxBodyBytes int64
	allowPrivate bool
}

func New(cfg Config) *Fetcher {
	if cfg.Timeout <= 0 {
		cfg.Timeout = 30 * time.Second
	}
	if cfg.MaxBodyBytes <= 0 {
		cfg.MaxBodyBytes = 10 << 20
	}
	if cfg.UserAgent == "" {
		cfg.UserAgent = "wre-fetcher/0.1"
	}
	return &Fetcher{
		client:       safehttp.NewClient(cfg.Timeout, cfg.AllowPrivate),
		userAgent:    cfg.UserAgent,
		maxBodyBytes: cfg.MaxBodyBytes,
		allowPrivate: cfg.AllowPrivate,
	}
}

func (f *Fetcher) Fetch(ctx context.Context, rawURL, method string, cached *CacheEntry) (Result, error) {
	parsed, err := url.Parse(rawURL)
	if err != nil {
		return Result{}, fmt.Errorf("invalid url: %w", err)
	}
	if err := safehttp.ValidateURL(parsed, f.allowPrivate); err != nil {
		return Result{}, err
	}
	if method == "" {
		method = http.MethodGet
	}
	if method != http.MethodGet && method != http.MethodHead {
		return Result{}, fmt.Errorf("unsupported method %q", method)
	}
	req, err := http.NewRequestWithContext(ctx, method, parsed.String(), nil)
	if err != nil {
		return Result{}, err
	}
	req.Header.Set("User-Agent", f.userAgent)
	req.Header.Set("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,application/json;q=0.8,text/plain;q=0.7,application/pdf;q=0.5,*/*;q=0.1")
	if cached != nil {
		if cached.ETag != "" {
			req.Header.Set("If-None-Match", cached.ETag)
		}
		if !cached.LastModified.IsZero() {
			req.Header.Set("If-Modified-Since", cached.LastModified.UTC().Format(http.TimeFormat))
		}
	}

	start := time.Now()
	resp, err := f.client.Do(req)
	if err != nil {
		return Result{}, err
	}
	defer func() { _ = resp.Body.Close() }()

	result := Result{
		RequestedURL: parsed.String(),
		FinalURL:     resp.Request.URL.String(),
		HTTPStatus:   resp.StatusCode,
		Header:       resp.Header.Clone(),
		ContentType:  resp.Header.Get("Content-Type"),
		ETag:         resp.Header.Get("ETag"),
		FetchedAt:    time.Now().UTC(),
		Duration:     time.Since(start),
		RetryAfter:   parseRetryAfter(resp.Header.Get("Retry-After")),
	}
	if modified := resp.Header.Get("Last-Modified"); modified != "" {
		if t, err := http.ParseTime(modified); err == nil {
			result.LastModified = t.UTC()
		}
	}
	if resp.StatusCode == http.StatusNotModified {
		result.NotModified = true
		return result, nil
	}

	if method != http.MethodHead {
		body, err := io.ReadAll(io.LimitReader(resp.Body, f.maxBodyBytes+1))
		if err != nil {
			return Result{}, err
		}
		if int64(len(body)) > f.maxBodyBytes {
			return Result{}, fmt.Errorf("response exceeds %d bytes", f.maxBodyBytes)
		}
		result.Body = body
		result.ContentKind = content.Detect(result.ContentType, body)
	}
	return result, nil
}

func parseRetryAfter(raw string) time.Duration {
	if raw == "" {
		return 0
	}
	if seconds, err := time.ParseDuration(raw + "s"); err == nil && seconds > 0 && seconds <= 24*time.Hour {
		return seconds
	}
	if t, err := http.ParseTime(raw); err == nil {
		if wait := time.Until(t); wait > 0 && wait <= 24*time.Hour {
			return wait
		}
	}
	return 0
}
