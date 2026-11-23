// Package archive writes immutable raw artifacts to S3-compatible storage.
package archive

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net/url"
	"strings"
	"time"

	"github.com/minio/minio-go/v7"
	"github.com/minio/minio-go/v7/pkg/credentials"

	"github.com/kushalsrinivas/web-research-ai-engine/internal/crawler/content"
)

type Config struct {
	Endpoint  string
	AccessKey string
	SecretKey string
	Bucket    string
	Region    string
}

type Record struct {
	TenantID       string
	SourceID       string
	DocumentID     string
	RequestedURL   string
	FinalURL       string
	HTTPStatus     int
	ContentType    string
	ContentKind    content.Kind
	FetchedAt      time.Time
	Body           []byte
	ResponseHeader map[string][]string
}

type Result struct {
	URI         string
	KeyPrefix   string
	ContentHash string
	Digest      []byte
	Bytes       int64
}

type Store interface {
	Archive(ctx context.Context, record Record) (Result, error)
}

type S3Store struct {
	client *minio.Client
	bucket string
}

func NewS3(ctx context.Context, cfg Config) (*S3Store, error) {
	parsed, err := url.Parse(cfg.Endpoint)
	if err != nil || parsed.Host == "" {
		return nil, fmt.Errorf("invalid S3_ENDPOINT")
	}
	client, err := minio.New(parsed.Host, &minio.Options{
		Creds:        credentials.NewStaticV4(cfg.AccessKey, cfg.SecretKey, ""),
		Secure:       parsed.Scheme == "https",
		Region:       cfg.Region,
		BucketLookup: minio.BucketLookupPath,
	})
	if err != nil {
		return nil, err
	}
	return &S3Store{client: client, bucket: cfg.Bucket}, nil
}

func (s *S3Store) Archive(ctx context.Context, record Record) (Result, error) {
	sum := sha256.Sum256(record.Body)
	contentHash := hex.EncodeToString(sum[:])
	source := record.SourceID
	if source == "" {
		source = "manual"
	}
	fetched := record.FetchedAt.UTC()
	prefix := fmt.Sprintf("raw/%s/%s/%04d/%02d/%02d/%s/%s",
		safe(record.TenantID), safe(source), fetched.Year(), fetched.Month(), fetched.Day(),
		safe(record.DocumentID), contentHash)
	originalKey := prefix + "/original" + record.ContentKind.Extension()
	responseKey := prefix + "/response.json"
	metadataKey := prefix + "/metadata.json"

	metadata := map[string]any{
		"tenant_id":      record.TenantID,
		"source_id":      record.SourceID,
		"document_id":    record.DocumentID,
		"requested_url":  record.RequestedURL,
		"final_url":      record.FinalURL,
		"http_status":    record.HTTPStatus,
		"content_type":   record.ContentType,
		"content_kind":   record.ContentKind,
		"content_hash":   contentHash,
		"bytes":          len(record.Body),
		"fetched_at":     fetched,
		"schema_version": 1,
	}
	metadataJSON, _ := json.Marshal(metadata)
	responseJSON, _ := json.Marshal(map[string]any{
		"requested_url": record.RequestedURL,
		"final_url":     record.FinalURL,
		"status":        record.HTTPStatus,
		"headers":       filterHeaders(record.ResponseHeader),
	})

	for _, object := range []struct {
		key         string
		data        []byte
		contentType string
	}{
		{originalKey, record.Body, record.ContentType},
		{responseKey, responseJSON, "application/json"},
		{metadataKey, metadataJSON, "application/json"},
	} {
		_, err := s.client.PutObject(ctx, s.bucket, object.key, bytes.NewReader(object.data),
			int64(len(object.data)), minio.PutObjectOptions{ContentType: object.contentType})
		if err != nil {
			return Result{}, fmt.Errorf("archive %s: %w", object.key, err)
		}
	}
	return Result{
		URI:         fmt.Sprintf("s3://%s/%s/original%s", s.bucket, prefix, record.ContentKind.Extension()),
		KeyPrefix:   prefix,
		ContentHash: contentHash,
		Digest:      sum[:],
		Bytes:       int64(len(record.Body)),
	}, nil
}

func safe(value string) string {
	value = strings.TrimSpace(value)
	if value == "" {
		return "unknown"
	}
	return strings.Map(func(r rune) rune {
		switch {
		case r >= 'a' && r <= 'z', r >= 'A' && r <= 'Z', r >= '0' && r <= '9', r == '-', r == '_', r == '.':
			return r
		default:
			return '-'
		}
	}, value)
}

func filterHeaders(headers map[string][]string) map[string][]string {
	blocked := map[string]struct{}{
		"set-cookie": {}, "cookie": {}, "authorization": {}, "proxy-authorization": {},
	}
	filtered := make(map[string][]string, len(headers))
	for key, values := range headers {
		if _, ok := blocked[strings.ToLower(key)]; ok {
			continue
		}
		filtered[key] = values
	}
	return filtered
}
