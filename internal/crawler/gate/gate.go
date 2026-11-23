// Package gate enforces per-host concurrency and request spacing inside one
// consumer partition owner. Kafka keys fetch jobs by host, so a host is owned
// by one worker at a time.
package gate

import (
	"context"
	"sync"
	"time"

	"github.com/kushalsrinivas/web-research-ai-engine/internal/crawlcontract"
)

type Gate struct {
	mu     sync.Mutex
	states map[string]*state
}

type state struct {
	mu       sync.Mutex
	inflight int
	next     time.Time
}

func New() *Gate {
	return &Gate{states: map[string]*state{}}
}

func (g *Gate) Acquire(ctx context.Context, host string, policy crawlcontract.DomainPolicy, robotsDelay time.Duration) (func(), error) {
	if policy.MaxConcurrency < 1 {
		policy.MaxConcurrency = 1
	}
	if policy.RequestsPerMinute < 1 {
		policy.RequestsPerMinute = 30
	}
	interval := time.Duration(policy.CrawlDelayMS) * time.Millisecond
	if perRequest := time.Minute / time.Duration(policy.RequestsPerMinute); perRequest > interval {
		interval = perRequest
	}
	if robotsDelay > interval {
		interval = robotsDelay
	}

	st := g.state(host)
	for {
		st.mu.Lock()
		now := time.Now()
		if wait := st.next.Sub(now); wait > 0 {
			st.mu.Unlock()
			if err := sleep(ctx, wait); err != nil {
				return nil, err
			}
			continue
		}
		if st.inflight >= policy.MaxConcurrency {
			st.mu.Unlock()
			if err := sleep(ctx, 25*time.Millisecond); err != nil {
				return nil, err
			}
			continue
		}
		st.inflight++
		st.mu.Unlock()

		return func() {
			st.mu.Lock()
			defer st.mu.Unlock()
			st.inflight--
			next := time.Now().Add(interval)
			if next.After(st.next) {
				st.next = next
			}
		}, nil
	}
}

func (g *Gate) state(host string) *state {
	g.mu.Lock()
	defer g.mu.Unlock()
	if st, ok := g.states[host]; ok {
		return st
	}
	st := &state{}
	g.states[host] = st
	return st
}

func sleep(ctx context.Context, d time.Duration) error {
	timer := time.NewTimer(d)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-timer.C:
		return nil
	}
}
