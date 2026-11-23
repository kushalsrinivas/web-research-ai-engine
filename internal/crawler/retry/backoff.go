// Package retry computes crawl retry delay.
package retry

import "time"

// Backoff returns an exponential delay capped at one hour. A server-provided
// Retry-After always wins, capped at 24 hours.
func Backoff(attempt int, retryAfter time.Duration) time.Duration {
	if attempt < 1 {
		attempt = 1
	}
	delay := 30 * time.Second
	for i := 1; i < attempt && delay < time.Hour; i++ {
		delay *= 2
	}
	if delay > time.Hour {
		delay = time.Hour
	}
	if retryAfter > delay {
		delay = retryAfter
	}
	if delay > 24*time.Hour {
		delay = 24 * time.Hour
	}
	return delay
}
