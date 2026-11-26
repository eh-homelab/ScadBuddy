//go:build unix

package telemetry

import "syscall"

// cpuTime is the process's user and system CPU seconds.
func cpuTime() (user, system float64, ok bool) {
	var u syscall.Rusage
	if syscall.Getrusage(syscall.RUSAGE_SELF, &u) != nil {
		return 0, 0, false
	}
	seconds := func(t syscall.Timeval) float64 { return float64(t.Sec) + float64(t.Usec)/1e6 }
	return seconds(u.Utime), seconds(u.Stime), true
}
