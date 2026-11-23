package gate

import (
	"context"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/kushalsrinivas/web-research-ai-engine/internal/crawlcontract"
)

func TestGateSerializesPerHost(t *testing.T) {
	g := New()
	policy := crawlcontract.DefaultDomainPolicy()
	policy.MaxConcurrency = 1
	policy.CrawlDelayMS = 0
	policy.RequestsPerMinute = 60000

	var active atomic.Int32
	var overlap atomic.Bool
	var wg sync.WaitGroup
	for i := 0; i < 5; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			release, err := g.Acquire(context.Background(), "example.com", policy, 0)
			if err != nil {
				t.Error(err)
				return
			}
			if active.Add(1) > 1 {
				overlap.Store(true)
			}
			time.Sleep(10 * time.Millisecond)
			active.Add(-1)
			release()
		}()
	}
	wg.Wait()
	if overlap.Load() {
		t.Fatal("per-host concurrency was exceeded")
	}
}

func TestGateSeparatesHosts(t *testing.T) {
	g := New()
	policy := crawlcontract.DefaultDomainPolicy()
	policy.MaxConcurrency = 1
	policy.RequestsPerMinute = 60000

	start := time.Now()
	var wg sync.WaitGroup
	for _, host := range []string{"a.example", "b.example"} {
		wg.Add(1)
		go func(host string) {
			defer wg.Done()
			release, err := g.Acquire(context.Background(), host, policy, 0)
			if err != nil {
				t.Error(err)
				return
			}
			time.Sleep(50 * time.Millisecond)
			release()
		}(host)
	}
	wg.Wait()
	if elapsed := time.Since(start); elapsed > 90*time.Millisecond {
		t.Fatalf("hosts were serialized: %s", elapsed)
	}
}
