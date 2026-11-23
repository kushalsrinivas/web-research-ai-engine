package content

import "testing"

func TestDetect(t *testing.T) {
	cases := []struct {
		name        string
		contentType string
		body        string
		want        Kind
	}{
		{"html header", "text/html; charset=utf-8", "<!doctype html><p>x", HTML},
		{"html sniffed", "application/octet-stream", "<html><body>x", HTML},
		{"pdf", "application/octet-stream", "%PDF-1.7", PDF},
		{"rss", "application/xml", `<?xml version="1.0"?><rss version="2.0"></rss>`, RSS},
		{"atom", "application/atom+xml", `<feed xmlns="http://www.w3.org/2005/Atom"></feed>`, Atom},
		{"json", "application/json", `{"a":1}`, JSON},
		{"text", "text/plain", "hello", Text},
		{"image", "image/png", "\x89PNG\r\n", Image},
	}
	for _, tc := range cases {
		if got := Detect(tc.contentType, []byte(tc.body)); got != tc.want {
			t.Fatalf("%s: Detect = %q, want %q", tc.name, got, tc.want)
		}
	}
}

func TestKindCapabilities(t *testing.T) {
	if !HTML.LinksAllowed() || !RSS.LinksAllowed() || JSON.LinksAllowed() {
		t.Fatal("unexpected link policy")
	}
	if !HTML.Textual() || PDF.Textual() {
		t.Fatal("unexpected textual policy")
	}
}
