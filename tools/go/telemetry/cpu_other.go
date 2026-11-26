//go:build !unix

package telemetry

// cpuTime is not read on this platform.
func cpuTime() (user, system float64, ok bool) { return 0, 0, false }
