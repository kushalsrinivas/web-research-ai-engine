package sources

import (
	"math"
	"testing"
	"time"
)

func TestComputeScoreHeuristicsAndOverrides(t *testing.T) {
	now := time.Date(2026, 11, 23, 10, 0, 0, 0, time.UTC)
	source := Source{
		ID:         "01JB2Q1H7S5D9F4K2M8N6P3R0T",
		SourceType: TypeGovernment,
		Metadata:   map[string]any{"topics": []string{"semiconductors"}},
	}
	score, err := ComputeScore(source, "", map[string]float64{"authority": 0.95}, nil, now)
	if err != nil {
		t.Fatal(err)
	}
	if score.ModelVersion != DefaultModelVersion {
		t.Fatalf("model version = %q", score.ModelVersion)
	}
	if score.Dimensions.Authority != 0.95 {
		t.Fatalf("authority = %v", score.Dimensions.Authority)
	}
	if score.Composite < 0 || score.Composite > 1 {
		t.Fatalf("composite = %v", score.Composite)
	}
	sum := score.Weights.Authority + score.Weights.Originality + score.Weights.HistoricalAccuracy +
		score.Weights.TopicalRelevance + score.Weights.PublicationFrequency + score.Weights.Freshness +
		score.Weights.Transparency + score.Weights.Corrections
	if math.Abs(sum-1) > 0.0001 {
		t.Fatalf("weights sum = %v", sum)
	}
}

func TestComputeScoreRejectsUnknownOrOutOfRangeDimensions(t *testing.T) {
	source := Source{SourceType: TypeNews}
	if _, err := ComputeScore(source, "v1", map[string]float64{"unknown": 0.5}, nil, time.Now()); err == nil {
		t.Fatal("expected unknown dimension error")
	}
	if _, err := ComputeScore(source, "v1", map[string]float64{"authority": 1.2}, nil, time.Now()); err == nil {
		t.Fatal("expected range error")
	}
}
