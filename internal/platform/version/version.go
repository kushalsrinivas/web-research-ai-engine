// Package version carries build metadata injected at link time.
package version

// Version is set with -ldflags "-X .../internal/platform/version.Version=<build>".
var Version = "dev"

// String returns the processor version stamped on derived data: service@build.
func String(service string) string {
	return service + "@" + Version
}
