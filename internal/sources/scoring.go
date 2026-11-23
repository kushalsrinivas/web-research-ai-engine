package sources

import (
	"fmt"
	"math"
	"time"
)

// DefaultWeights are source-quality-v1 weights. They sum to 1.0.
var DefaultWeights = Weights{
	Authority:            0.20,
	Originality:          0.15,
	HistoricalAccuracy:   0.15,
	TopicalRelevance:     0.15,
	PublicationFrequency: 0.10,
	Freshness:            0.10,
	Transparency:         0.10,
	Corrections:          0.05,
}

const DefaultModelVersion = "source-quality-v1"

var authorityByType = map[string]float64{
	TypeGovernment:   0.90,
	TypeResearch:     0.80,
	TypeFinancial:    0.80,
	TypeDataset:      0.75,
	TypePressRelease: 0.70,
	TypeNews:         0.65,
	TypeRSS:          0.55,
	TypeWebsite:      0.50,
	TypePDF:          0.50,
	TypeForum:        0.30,
}

var transparencyByType = map[string]float64{
	TypeGovernment:   0.85,
	TypeResearch:     0.80,
	TypeFinancial:    0.75,
	TypePressRelease: 0.65,
	TypeNews:         0.55,
	TypeRSS:          0.45,
	TypeWebsite:      0.45,
	TypeDataset:      0.70,
	TypePDF:          0.40,
	TypeForum:        0.30,
}

// HeuristicDimensions provides explainable starting values when no external
// evaluation exists. They are not evidence that a source is correct.
func HeuristicDimensions(s Source, now time.Time) Dimensions {
	authority := authorityByType[s.SourceType]
	if authority == 0 {
		authority = 0.5
	}
	transparency := transparencyByType[s.SourceType]
	if transparency == 0 {
		transparency = 0.5
	}
	publication := 0.5
	switch {
	case s.CrawlFrequencySeconds != nil && *s.CrawlFrequencySeconds <= 15*60:
		publication = 0.90
	case s.CrawlFrequencySeconds != nil && *s.CrawlFrequencySeconds <= 60*60:
		publication = 0.80
	case s.CrawlFrequencySeconds != nil && *s.CrawlFrequencySeconds <= 6*60*60:
		publication = 0.60
	case s.CrawlFrequencySeconds != nil && *s.CrawlFrequencySeconds <= 24*60*60:
		publication = 0.40
	case s.SourceType == TypeNews:
		publication = 0.85
	case s.SourceType == TypeRSS:
		publication = 0.70
	case s.SourceType == TypeWebsite:
		publication = 0.35
	}

	freshness := 0.5
	if s.LastCrawledAt != nil {
		age := now.Sub(*s.LastCrawledAt)
		switch {
		case age <= time.Hour:
			freshness = 0.95
		case age <= 24*time.Hour:
			freshness = 0.75
		case age <= 7*24*time.Hour:
			freshness = 0.45
		default:
			freshness = 0.20
		}
	}

	topical := 0.5
	for _, key := range []string{"topics", "topic", "queries", "categories"} {
		if value, ok := s.Metadata[key]; ok && value != nil {
			topical = 0.75
			break
		}
	}

	originality := 0.5
	switch s.SourceType {
	case TypePressRelease:
		originality = 0.75
	case TypeGovernment, TypeResearch, TypeFinancial:
		originality = 0.70
	case TypeNews:
		originality = 0.60
	case TypeRSS:
		originality = 0.35
	case TypeForum:
		originality = 0.45
	}

	return Dimensions{
		Authority:            authority,
		Originality:          originality,
		HistoricalAccuracy:   0.5,
		TopicalRelevance:     topical,
		PublicationFrequency: publication,
		Freshness:            freshness,
		Transparency:         transparency,
		Corrections:          0.5,
	}
}

// ComputeScore merges optional evaluator overrides with heuristics and
// persists nothing itself; the repository records the returned score.
func ComputeScore(source Source, modelVersion string, overrides map[string]float64, details map[string]any, now time.Time) (Score, error) {
	if modelVersion == "" {
		modelVersion = DefaultModelVersion
	}
	dims := HeuristicDimensions(source, now)
	for name, value := range overrides {
		if math.IsNaN(value) || value < 0 || value > 1 {
			return Score{}, fmt.Errorf("dimension %s must be between 0 and 1", name)
		}
		switch name {
		case "authority":
			dims.Authority = value
		case "originality":
			dims.Originality = value
		case "historical_accuracy":
			dims.HistoricalAccuracy = value
		case "topical_relevance":
			dims.TopicalRelevance = value
		case "publication_frequency":
			dims.PublicationFrequency = value
		case "freshness":
			dims.Freshness = value
		case "transparency":
			dims.Transparency = value
		case "corrections":
			dims.Corrections = value
		default:
			return Score{}, fmt.Errorf("unknown quality dimension %q", name)
		}
	}
	if details == nil {
		details = map[string]any{}
	}
	details["source_type"] = source.SourceType
	details["heuristic_baseline"] = true

	return Score{
		SourceID:     source.ID,
		ModelVersion: modelVersion,
		EvaluatedAt:  now.UTC(),
		Dimensions:   dims,
		Composite:    round4(composite(dims, DefaultWeights)),
		Weights:      DefaultWeights,
		Details:      details,
	}, nil
}

func composite(d Dimensions, w Weights) float64 {
	return d.Authority*w.Authority +
		d.Originality*w.Originality +
		d.HistoricalAccuracy*w.HistoricalAccuracy +
		d.TopicalRelevance*w.TopicalRelevance +
		d.PublicationFrequency*w.PublicationFrequency +
		d.Freshness*w.Freshness +
		d.Transparency*w.Transparency +
		d.Corrections*w.Corrections
}

func round4(v float64) float64 {
	return math.Round(v*10000) / 10000
}
