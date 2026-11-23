// Package contracts verifies that the versioned event schemas can be consumed
// by Go and match the shared fixtures in /contracts.
package contracts

import (
	"encoding/json"
	"io/fs"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/santhosh-tekuri/jsonschema/v5"
)

const contractsRoot = "../../contracts"

var payloadSchemaFor = map[string]string{
	"document.scheduled":   "document.scheduled.schema.json",
	"document.fetched":     "document.fetched.schema.json",
	"document.parsed":      "document.parsed.schema.json",
	"document.enriched":    "document.enriched.schema.json",
	"document.indexed":     "document.gate.schema.json",
	"document.kg_updated":  "document.gate.schema.json",
	"document.available":   "document.available.schema.json",
	"document.failed":      "document.failed.schema.json",
	"document.dead_letter": "document.dead_letter.schema.json",
	"document.duplicate":   "document.duplicate.schema.json",
	"document.reprocess":   "document.reprocess.schema.json",
}

func TestEverySchemaCompiles(t *testing.T) {
	schemas := schemaFiles(t)
	if len(schemas) == 0 {
		t.Fatal("no event schemas found")
	}
	for _, path := range schemas {
		t.Run(filepath.Base(path), func(t *testing.T) {
			compileSchema(t, path)
		})
	}
}

func TestValidFixturesMatchEnvelopeAndPayload(t *testing.T) {
	envelope := compileSchema(t, filepath.Join(contractsRoot, "events", "v1", "envelope.schema.json"))
	fixtures, err := filepath.Glob(filepath.Join(contractsRoot, "fixtures", "events", "*.valid.json"))
	if err != nil {
		t.Fatal(err)
	}
	if len(fixtures) == 0 {
		t.Fatal("no event fixtures found")
	}

	for _, path := range fixtures {
		t.Run(filepath.Base(path), func(t *testing.T) {
			var envelopeValue map[string]any
			readJSON(t, path, &envelopeValue)
			if err := envelope.Validate(envelopeValue); err != nil {
				t.Fatalf("envelope validation failed: %v", err)
			}

			eventType, _ := envelopeValue["event_type"].(string)
			payloadFile, ok := payloadSchemaFor[eventType]
			if !ok {
				t.Fatalf("no payload schema mapped for %q", eventType)
			}
			payload, ok := envelopeValue["payload"].(map[string]any)
			if !ok {
				t.Fatalf("payload is not an object")
			}
			schema := compileSchema(t, filepath.Join(contractsRoot, "events", "v1", "payloads", payloadFile))
			if err := schema.Validate(payload); err != nil {
				t.Fatalf("payload validation failed for %s: %v", eventType, err)
			}
		})
	}
}

func TestInvalidFixturesAreRejected(t *testing.T) {
	envelope := compileSchema(t, filepath.Join(contractsRoot, "events", "v1", "envelope.schema.json"))
	fixtures, err := filepath.Glob(filepath.Join(contractsRoot, "fixtures", "events", "invalid", "*.json"))
	if err != nil {
		t.Fatal(err)
	}
	if len(fixtures) == 0 {
		t.Fatal("no invalid fixtures found")
	}

	for _, path := range fixtures {
		t.Run(filepath.Base(path), func(t *testing.T) {
			var value map[string]any
			readJSON(t, path, &value)
			if err := envelope.Validate(value); err == nil {
				t.Fatalf("expected fixture to be rejected: %s", path)
			}
		})
	}
}

func compileSchema(t *testing.T, path string) *jsonschema.Schema {
	t.Helper()
	file, err := os.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer file.Close()

	compiler := jsonschema.NewCompiler()
	if err := compiler.AddResource("schema.json", file); err != nil {
		t.Fatalf("add schema %s: %v", path, err)
	}
	schema, err := compiler.Compile("schema.json")
	if err != nil {
		t.Fatalf("compile schema %s: %v", path, err)
	}
	return schema
}

func readJSON(t *testing.T, path string, target any) {
	t.Helper()
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal(data, target); err != nil {
		t.Fatalf("parse %s: %v", path, err)
	}
}

func schemaFiles(t *testing.T) []string {
	t.Helper()
	var files []string
	err := filepath.WalkDir(filepath.Join(contractsRoot, "events", "v1"), func(path string, entry fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if !entry.IsDir() && strings.HasSuffix(path, ".schema.json") {
			files = append(files, path)
		}
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	return files
}
