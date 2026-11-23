package sources

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"strconv"
	"time"

	"github.com/go-chi/chi/v5"
	"github.com/google/uuid"
	"go.opentelemetry.io/otel/trace"
)

const maxRequestBytes = 1 << 20

// Handler exposes the source registry over the scheduler's internal API.
type Handler struct {
	repo      *Repository
	discovery *Discovery
	metrics   *Metrics
	logger    *slog.Logger
}

func NewHandler(repo *Repository, discovery *Discovery, metrics *Metrics, logger *slog.Logger) *Handler {
	return &Handler{repo: repo, discovery: discovery, metrics: metrics, logger: logger}
}

// Mount attaches source and domain-policy routes to the shared router.
func (h *Handler) Mount(r chi.Router) {
	r.Route("/internal/v1", func(r chi.Router) {
		r.Route("/sources", func(r chi.Router) {
			r.Get("/", h.listSources)
			r.Post("/", h.createSource)
			r.Get("/due", h.dueSources)
			r.Get("/health", h.listHealth)
			r.Get("/discoveries", h.listDiscoveries)
			r.Post("/discover/seed", h.discoverSeed)
			r.Post("/discover/topic", h.discoverTopic)
			r.Post("/discover/graph", h.discoverGraph)
			r.Route("/{id}", func(r chi.Router) {
				r.Get("/", h.getSource)
				r.Patch("/", h.updateSource)
				r.Delete("/", h.deleteSource)
				r.Get("/health", h.getHealth)
				r.Get("/history", h.getHistory)
				r.Get("/score", h.getScore)
				r.Post("/score", h.scoreSource)
				r.Get("/versions", h.getVersions)
				r.Post("/outcomes", h.recordOutcome)
			})
		})
		r.Route("/domain-policies", func(r chi.Router) {
			r.Get("/", h.listPolicies)
			r.Put("/{domain}", h.upsertPolicy)
			r.Delete("/{domain}", h.deletePolicy)
		})
	})
}

type createSourceRequest struct {
	Name                  string         `json:"name"`
	BaseURL               string         `json:"base_url"`
	SourceType            string         `json:"source_type"`
	Domain                string         `json:"domain"`
	Language              string         `json:"language"`
	CrawlFrequencySeconds *int           `json:"crawl_frequency_seconds"`
	Priority              *int           `json:"priority"`
	RobotsPolicy          string         `json:"robots_policy"`
	CrawlEnabled          *bool          `json:"crawl_enabled"`
	Status                string         `json:"status"`
	Metadata              map[string]any `json:"metadata"`
}

func (h *Handler) createSource(w http.ResponseWriter, r *http.Request) {
	var req createSourceRequest
	if !decodeJSON(w, r, &req) {
		return
	}
	priority := 5
	if req.Priority != nil {
		priority = *req.Priority
	}
	enabled := true
	if req.CrawlEnabled != nil {
		enabled = *req.CrawlEnabled
	}
	source := Source{
		Name:                  req.Name,
		BaseURL:               req.BaseURL,
		SourceType:            req.SourceType,
		Domain:                req.Domain,
		Language:              req.Language,
		CrawlFrequencySeconds: req.CrawlFrequencySeconds,
		Priority:              priority,
		RobotsPolicy:          req.RobotsPolicy,
		CrawlEnabled:          enabled,
		Status:                req.Status,
		Metadata:              req.Metadata,
	}
	if err := ValidateSource(&source); err != nil {
		writeProblem(w, r, http.StatusUnprocessableEntity, err.Error())
		return
	}
	created, err := h.repo.CreateSource(r.Context(), source, "api")
	if err != nil {
		h.writeRepoError(w, r, err)
		return
	}
	writeJSON(w, http.StatusCreated, created)
}

func (h *Handler) getSource(w http.ResponseWriter, r *http.Request) {
	id, ok := sourceID(w, r)
	if !ok {
		return
	}
	source, err := h.repo.GetSource(r.Context(), id)
	if err != nil {
		h.writeRepoError(w, r, err)
		return
	}
	writeJSON(w, http.StatusOK, source)
}

func (h *Handler) listSources(w http.ResponseWriter, r *http.Request) {
	filter := SourceFilter{
		Status:     r.URL.Query().Get("status"),
		SourceType: r.URL.Query().Get("source_type"),
		Domain:     r.URL.Query().Get("domain"),
		Limit:      queryInt(r, "limit", 50),
		Offset:     queryInt(r, "offset", 0),
	}
	if filter.Status != "" {
		if _, ok := allowedStatuses[filter.Status]; !ok {
			writeProblem(w, r, http.StatusUnprocessableEntity, "invalid status filter")
			return
		}
	}
	if filter.SourceType != "" {
		if _, ok := allowedSourceTypes[filter.SourceType]; !ok {
			writeProblem(w, r, http.StatusUnprocessableEntity, "invalid source_type filter")
			return
		}
	}
	items, total, err := h.repo.ListSources(r.Context(), filter)
	if err != nil {
		h.writeRepoError(w, r, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"items": items, "total": total, "limit": ClampLimit(filter.Limit), "offset": filter.Offset,
	})
}

func (h *Handler) updateSource(w http.ResponseWriter, r *http.Request) {
	id, ok := sourceID(w, r)
	if !ok {
		return
	}
	var patch SourcePatch
	if !decodeJSON(w, r, &patch) {
		return
	}
	source, err := h.repo.GetSource(r.Context(), id)
	if err != nil {
		h.writeRepoError(w, r, err)
		return
	}
	applyPatch(&source, patch)
	if err := ValidateSource(&source); err != nil {
		writeProblem(w, r, http.StatusUnprocessableEntity, err.Error())
		return
	}
	updated, err := h.repo.UpdateSource(r.Context(), source, "api")
	if err != nil {
		h.writeRepoError(w, r, err)
		return
	}
	writeJSON(w, http.StatusOK, updated)
}

func (h *Handler) deleteSource(w http.ResponseWriter, r *http.Request) {
	id, ok := sourceID(w, r)
	if !ok {
		return
	}
	if err := h.repo.DeleteSource(r.Context(), id); err != nil {
		h.writeRepoError(w, r, err)
		return
	}
	w.WriteHeader(http.StatusNoContent)
}

func (h *Handler) dueSources(w http.ResponseWriter, r *http.Request) {
	items, err := h.repo.DueSources(r.Context(), time.Now().UTC(), queryInt(r, "limit", 50))
	if err != nil {
		h.writeRepoError(w, r, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"items": items, "count": len(items)})
}

func (h *Handler) recordOutcome(w http.ResponseWriter, r *http.Request) {
	id, ok := sourceID(w, r)
	if !ok {
		return
	}
	var outcome CrawlOutcome
	if !decodeJSON(w, r, &outcome) {
		return
	}
	if err := ValidateCrawlOutcome(&outcome); err != nil {
		writeProblem(w, r, http.StatusUnprocessableEntity, err.Error())
		return
	}
	source, health, err := h.repo.RecordOutcome(r.Context(), id, outcome)
	if err != nil {
		h.writeRepoError(w, r, err)
		return
	}
	if h.metrics != nil {
		h.metrics.ObserveCrawlOutcome(outcome.Status)
	}
	writeJSON(w, http.StatusCreated, map[string]any{"source": source, "health": health})
}

type scoreRequest struct {
	ModelVersion string             `json:"model_version"`
	Dimensions   map[string]float64 `json:"dimensions"`
	Details      map[string]any     `json:"details"`
}

func (h *Handler) scoreSource(w http.ResponseWriter, r *http.Request) {
	id, ok := sourceID(w, r)
	if !ok {
		return
	}
	var req scoreRequest
	if !decodeJSON(w, r, &req) {
		return
	}
	source, err := h.repo.GetSource(r.Context(), id)
	if err != nil {
		h.writeRepoError(w, r, err)
		return
	}
	score, err := ComputeScore(source, req.ModelVersion, req.Dimensions, req.Details, time.Now().UTC())
	if err != nil {
		writeProblem(w, r, http.StatusUnprocessableEntity, err.Error())
		return
	}
	saved, err := h.repo.SaveScore(r.Context(), score)
	if err != nil {
		h.writeRepoError(w, r, err)
		return
	}
	if h.metrics != nil {
		h.metrics.ObserveScore(saved.ModelVersion)
	}
	writeJSON(w, http.StatusCreated, saved)
}

func (h *Handler) getScore(w http.ResponseWriter, r *http.Request) {
	id, ok := sourceID(w, r)
	if !ok {
		return
	}
	score, err := h.repo.LatestScore(r.Context(), id)
	if err != nil {
		h.writeRepoError(w, r, err)
		return
	}
	writeJSON(w, http.StatusOK, score)
}

func (h *Handler) getHealth(w http.ResponseWriter, r *http.Request) {
	id, ok := sourceID(w, r)
	if !ok {
		return
	}
	health, err := h.repo.Health(r.Context(), id)
	if err != nil {
		h.writeRepoError(w, r, err)
		return
	}
	writeJSON(w, http.StatusOK, health)
}

func (h *Handler) listHealth(w http.ResponseWriter, r *http.Request) {
	items, err := h.repo.ListHealth(r.Context())
	if err != nil {
		h.writeRepoError(w, r, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"items": items, "count": len(items)})
}

func (h *Handler) getHistory(w http.ResponseWriter, r *http.Request) {
	id, ok := sourceID(w, r)
	if !ok {
		return
	}
	if _, err := h.repo.GetSource(r.Context(), id); err != nil {
		h.writeRepoError(w, r, err)
		return
	}
	items, err := h.repo.CrawlHistory(r.Context(), id, queryInt(r, "limit", 20))
	if err != nil {
		h.writeRepoError(w, r, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"items": items, "count": len(items)})
}

func (h *Handler) getVersions(w http.ResponseWriter, r *http.Request) {
	id, ok := sourceID(w, r)
	if !ok {
		return
	}
	if _, err := h.repo.GetSource(r.Context(), id); err != nil {
		h.writeRepoError(w, r, err)
		return
	}
	items, err := h.repo.ConfigVersions(r.Context(), id)
	if err != nil {
		h.writeRepoError(w, r, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"items": items})
}

func (h *Handler) listPolicies(w http.ResponseWriter, r *http.Request) {
	items, err := h.repo.ListDomainPolicies(r.Context())
	if err != nil {
		h.writeRepoError(w, r, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"items": items, "count": len(items)})
}

type policyRequest struct {
	MaxConcurrency    int            `json:"max_concurrency"`
	RequestsPerMinute int            `json:"requests_per_minute"`
	CrawlDelayMS      int            `json:"crawl_delay_ms"`
	RobotsPolicy      string         `json:"robots_policy"`
	Allowed           *bool          `json:"allowed"`
	Notes             string         `json:"notes"`
	Metadata          map[string]any `json:"metadata"`
}

func (h *Handler) upsertPolicy(w http.ResponseWriter, r *http.Request) {
	var req policyRequest
	if !decodeJSON(w, r, &req) {
		return
	}
	policy := DomainPolicy{
		Domain:            chi.URLParam(r, "domain"),
		MaxConcurrency:    req.MaxConcurrency,
		RequestsPerMinute: req.RequestsPerMinute,
		CrawlDelayMS:      req.CrawlDelayMS,
		RobotsPolicy:      req.RobotsPolicy,
		Allowed:           true,
		Notes:             req.Notes,
		Metadata:          req.Metadata,
	}
	if req.Allowed != nil {
		policy.Allowed = *req.Allowed
	}
	if err := ValidateDomainPolicy(&policy); err != nil {
		writeProblem(w, r, http.StatusUnprocessableEntity, err.Error())
		return
	}
	saved, err := h.repo.UpsertDomainPolicy(r.Context(), policy)
	if err != nil {
		h.writeRepoError(w, r, err)
		return
	}
	writeJSON(w, http.StatusOK, saved)
}

func (h *Handler) deletePolicy(w http.ResponseWriter, r *http.Request) {
	if err := h.repo.DeleteDomainPolicy(r.Context(), chi.URLParam(r, "domain")); err != nil {
		h.writeRepoError(w, r, err)
		return
	}
	w.WriteHeader(http.StatusNoContent)
}

type discoveryRequest struct {
	URL           string   `json:"url"`
	Topic         string   `json:"topic"`
	Queries       []string `json:"queries"`
	SourceID      string   `json:"source_id"`
	MaxCandidates int      `json:"max_candidates"`
	Register      bool     `json:"register"`
}

func (h *Handler) discoverSeed(w http.ResponseWriter, r *http.Request) {
	h.runDiscovery(w, r, "seed", func(ctx context.Context, req discoveryRequest) ([]Candidate, error) {
		_, domain, err := NormalizeBaseURL(req.URL)
		if err != nil {
			return nil, fmt.Errorf("%w: %v", ErrInvalidRequest, err)
		}
		if err := h.checkDomainPolicy(ctx, domain); err != nil {
			return nil, err
		}
		return h.discovery.DiscoverSeed(ctx, req.URL, req.MaxCandidates)
	})
}

func (h *Handler) discoverTopic(w http.ResponseWriter, r *http.Request) {
	h.runDiscovery(w, r, "topic", func(ctx context.Context, req discoveryRequest) ([]Candidate, error) {
		return h.discovery.DiscoverTopic(ctx, req.Topic, req.Queries, req.MaxCandidates)
	})
}

func (h *Handler) discoverGraph(w http.ResponseWriter, r *http.Request) {
	h.runDiscovery(w, r, "graph", func(ctx context.Context, req discoveryRequest) ([]Candidate, error) {
		if _, err := uuid.Parse(req.SourceID); err != nil {
			return nil, fmt.Errorf("%w: source_id must be a UUID", ErrInvalidRequest)
		}
		source, err := h.repo.GetSource(ctx, req.SourceID)
		if err != nil {
			return nil, err
		}
		if err := h.checkDomainPolicy(ctx, source.Domain); err != nil {
			return nil, err
		}
		return h.discovery.DiscoverGraph(ctx, source, req.MaxCandidates)
	})
}

func (h *Handler) runDiscovery(w http.ResponseWriter, r *http.Request, mechanism string, run func(context.Context, discoveryRequest) ([]Candidate, error)) {
	var req discoveryRequest
	if !decodeJSON(w, r, &req) {
		return
	}
	var input map[string]any
	raw, _ := json.Marshal(req)
	_ = json.Unmarshal(raw, &input)

	candidates, err := run(r.Context(), req)
	if err != nil {
		_, _ = h.repo.RecordDiscovery(r.Context(), mechanism, input, []Candidate{}, 0, err)
		if h.metrics != nil {
			h.metrics.ObserveDiscovery(mechanism, "error")
		}
		switch {
		case errors.Is(err, ErrInvalidRequest):
			writeProblem(w, r, http.StatusBadRequest, err.Error())
		case errors.Is(err, ErrPolicyDenied), errors.Is(err, ErrRobotsDenied):
			writeProblem(w, r, http.StatusUnprocessableEntity, err.Error())
		case errors.Is(err, ErrProviderUnavailable):
			writeProblem(w, r, http.StatusNotImplemented, err.Error())
		case errors.Is(err, ErrNotFound):
			writeProblem(w, r, http.StatusNotFound, err.Error())
		default:
			h.logger.ErrorContext(r.Context(), "discovery failed", "mechanism", mechanism, "error", err)
			writeProblem(w, r, http.StatusBadGateway, "discovery failed: "+err.Error())
		}
		return
	}
	registered, registerErr := h.registerCandidates(r.Context(), candidates, req.Register)
	if registerErr != nil {
		if h.metrics != nil {
			h.metrics.ObserveDiscovery(mechanism, "error")
		}
		h.writeRepoError(w, r, registerErr)
		return
	}
	id, recordErr := h.repo.RecordDiscovery(r.Context(), mechanism, input, candidates, len(registered), nil)
	if recordErr != nil {
		if h.metrics != nil {
			h.metrics.ObserveDiscovery(mechanism, "error")
		}
		h.writeRepoError(w, r, recordErr)
		return
	}
	if h.metrics != nil {
		h.metrics.ObserveDiscovery(mechanism, "success")
	}
	writeJSON(w, http.StatusOK, DiscoveryResult{
		ID: id, Mechanism: mechanism, Candidates: candidates, Registered: registered,
	})
}

func (h *Handler) registerCandidates(ctx context.Context, candidates []Candidate, register bool) ([]Source, error) {
	if !register {
		return nil, nil
	}
	created := make([]Source, 0, len(candidates))
	for _, candidate := range candidates {
		if err := h.checkDomainPolicy(ctx, candidate.Domain); err != nil {
			continue
		}
		source, wasCreated, err := h.repo.CreateCandidate(ctx, candidate, "discovery")
		if err != nil {
			h.logger.WarnContext(ctx, "candidate registration failed", "base_url", candidate.BaseURL, "error", err)
			continue
		}
		if wasCreated {
			created = append(created, source)
		}
	}
	return created, nil
}

func (h *Handler) checkDomainPolicy(ctx context.Context, domain string) error {
	if domain == "" {
		return nil
	}
	policy, err := h.repo.GetDomainPolicy(ctx, domain)
	if errors.Is(err, ErrNotFound) {
		return nil
	}
	if err != nil {
		return err
	}
	if !policy.Allowed || policy.RobotsPolicy == RobotsDeny {
		return ErrPolicyDenied
	}
	return nil
}

func (h *Handler) listDiscoveries(w http.ResponseWriter, r *http.Request) {
	items, err := h.repo.ListDiscoveries(r.Context(), queryInt(r, "limit", 50))
	if err != nil {
		h.writeRepoError(w, r, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"items": items, "count": len(items)})
}

func applyPatch(source *Source, patch SourcePatch) {
	if patch.Name != nil {
		source.Name = *patch.Name
	}
	if patch.BaseURL != nil {
		source.BaseURL = *patch.BaseURL
	}
	if patch.SourceType != nil {
		source.SourceType = *patch.SourceType
	}
	if patch.Domain != nil {
		source.Domain = *patch.Domain
	}
	if patch.Language != nil {
		source.Language = *patch.Language
	}
	if patch.CrawlFrequencySeconds != nil {
		source.CrawlFrequencySeconds = patch.CrawlFrequencySeconds
	}
	if patch.Priority != nil {
		source.Priority = *patch.Priority
	}
	if patch.RobotsPolicy != nil {
		source.RobotsPolicy = *patch.RobotsPolicy
	}
	if patch.CrawlEnabled != nil {
		source.CrawlEnabled = *patch.CrawlEnabled
	}
	if patch.Status != nil {
		source.Status = *patch.Status
	}
	if patch.Metadata != nil {
		source.Metadata = patch.Metadata
	}
}

func sourceID(w http.ResponseWriter, r *http.Request) (string, bool) {
	id := chi.URLParam(r, "id")
	if _, err := uuid.Parse(id); err != nil {
		writeProblem(w, r, http.StatusBadRequest, "source id must be a UUID")
		return "", false
	}
	return id, true
}

func queryInt(r *http.Request, key string, fallback int) int {
	raw := r.URL.Query().Get(key)
	if raw == "" {
		return fallback
	}
	value, err := strconv.Atoi(raw)
	if err != nil {
		return fallback
	}
	return value
}

func decodeJSON(w http.ResponseWriter, r *http.Request, target any) bool {
	r.Body = http.MaxBytesReader(w, r.Body, maxRequestBytes)
	decoder := json.NewDecoder(r.Body)
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(target); err != nil {
		writeProblem(w, r, http.StatusBadRequest, "invalid JSON body: "+err.Error())
		return false
	}
	return true
}

func writeJSON(w http.ResponseWriter, status int, body any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(body)
}

func writeProblem(w http.ResponseWriter, r *http.Request, status int, detail string) {
	body := map[string]any{
		"type":   "about:blank",
		"title":  http.StatusText(status),
		"status": status,
		"detail": detail,
	}
	if span := trace.SpanContextFromContext(r.Context()); span.IsValid() {
		body["trace_id"] = span.TraceID().String()
	}
	w.Header().Set("Content-Type", "application/problem+json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(body)
}

func (h *Handler) writeRepoError(w http.ResponseWriter, r *http.Request, err error) {
	switch {
	case errors.Is(err, ErrNotFound):
		writeProblem(w, r, http.StatusNotFound, err.Error())
	case errors.Is(err, ErrConflict):
		writeProblem(w, r, http.StatusConflict, err.Error())
	case errors.Is(err, ErrRobotsDenied), errors.Is(err, ErrPolicyDenied):
		writeProblem(w, r, http.StatusUnprocessableEntity, err.Error())
	case errors.Is(err, ErrProviderUnavailable):
		writeProblem(w, r, http.StatusNotImplemented, err.Error())
	default:
		h.logger.ErrorContext(r.Context(), "source registry request failed", "error", err)
		writeProblem(w, r, http.StatusInternalServerError, "source registry request failed")
	}
}
