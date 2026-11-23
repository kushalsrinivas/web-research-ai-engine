package retry

import (
	"testing"
	"time"
)

func TestBackoffGrowsAndCaps(t *testing.T) {
	if got := Backoff(1, 0); got != 30*time.Second {
		t.Fatalf("attempt 1 = %s", got)
	}
	if got := Backoff(3, 0); got != 2*time.Minute {
		t.Fatalf("attempt 3 = %s", got)
	}
	if got := Backoff(20, 0); got != time.Hour {
		t.Fatalf("attempt 20 = %s, want cap", got)
	}
}

func TestBackoffHonorsRetryAfter(t *testing.T) {
	if got := Backoff(1, 10*time.Minute); got != 10*time.Minute {
		t.Fatalf("retry-after = %s", got)
	}
	if got := Backoff(1, 48*time.Hour); got != 24*time.Hour {
		t.Fatalf("retry-after cap = %s", got)
	}
	if got := Backoff(0, 5*time.Second); got != 30*time.Second {
		t.Fatalf("short retry-after must not shorten backoff: %s", got)
	}
}
