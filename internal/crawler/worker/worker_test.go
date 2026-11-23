package worker

import "testing"

func TestRetryableStatus(t *testing.T) {
	cases := map[int]bool{
		200: false,
		304: false,
		400: false,
		404: false,
		408: true,
		429: true,
		500: true,
		503: true,
	}
	for status, want := range cases {
		if got := retryableStatus(status); got != want {
			t.Fatalf("retryableStatus(%d) = %v, want %v", status, got, want)
		}
	}
}

func TestOutcomeFor(t *testing.T) {
	cases := map[int]string{
		200: "success",
		304: "not_modified",
		404: "client_error",
		500: "server_error",
	}
	for status, want := range cases {
		if got := outcomeFor(status); got != want {
			t.Fatalf("outcomeFor(%d) = %q, want %q", status, got, want)
		}
	}
}
