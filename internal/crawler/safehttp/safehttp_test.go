package safehttp

import (
	"net"
	"net/url"
	"testing"
)

func TestIsPublic(t *testing.T) {
	cases := map[string]bool{
		"8.8.8.8":         true,
		"1.1.1.1":         true,
		"127.0.0.1":       false,
		"10.1.2.3":        false,
		"172.16.0.1":      false,
		"192.168.0.1":     false,
		"169.254.169.254": false,
		"100.64.0.1":      false,
		"::1":             false,
		"fd00::1":         false,
	}
	for raw, want := range cases {
		if got := IsPublic(net.ParseIP(raw)); got != want {
			t.Fatalf("IsPublic(%s) = %v, want %v", raw, got, want)
		}
	}
}

func TestValidateURL(t *testing.T) {
	cases := []struct {
		raw          string
		allowPrivate bool
		ok           bool
	}{
		{"https://example.com/a", false, true},
		{"http://127.0.0.1/a", false, false},
		{"http://127.0.0.1:8080/a", false, false},
		{"http://10.0.0.1/a", false, false},
		{"http://169.254.169.254/latest/meta-data", false, false},
		{"file:///etc/passwd", false, false},
		{"https://user:pass@example.com/", false, false},
		{"http://example.com:8080/a", false, false},
		{"http://127.0.0.1:8080/a", true, true},
	}
	for _, tc := range cases {
		parsed, err := url.Parse(tc.raw)
		if err != nil {
			t.Fatal(err)
		}
		if err := ValidateURL(parsed, tc.allowPrivate); (err == nil) != tc.ok {
			t.Fatalf("ValidateURL(%q, %v) = %v", tc.raw, tc.allowPrivate, err)
		}
	}
}
