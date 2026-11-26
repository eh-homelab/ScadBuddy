package telemetry

import (
	"bytes"
	"context"
	"os"
	"runtime/debug"
	rtmetrics "runtime/metrics"
	"strconv"
	"strings"
	"time"

	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/metric"
)

// started is when the process started, as near as this package can tell.
var started = time.Now()

// memoryClasses are runtime/metrics' /memory/classes/*:bytes, which sum to the memory the
// runtime has mapped.
var memoryClasses = func() []rtmetrics.Sample {
	var samples []rtmetrics.Sample
	for _, d := range rtmetrics.All() {
		if strings.HasPrefix(d.Name, "/memory/classes/") && d.Kind == rtmetrics.KindUint64 &&
			d.Name != "/memory/classes/total:bytes" {
			samples = append(samples, rtmetrics.Sample{Name: d.Name})
		}
	}
	return samples
}()

// startProcess registers the process's metrics, with the semantic conventions' names, and
// the GC's cycles, pauses and memory classes, which contrib's runtime instrumentation
// gives only under a deprecated flag. A value the platform does not give (no /proc) is
// not observed.
func startProcess(mp metric.MeterProvider) error {
	m := mp.Meter(Scope + "/process")
	cpu, err := m.Float64ObservableCounter("process.cpu.time", metric.WithUnit("s"),
		metric.WithDescription("Total CPU seconds broken down by different CPU modes."))
	if err != nil {
		return err
	}
	rss, err := m.Int64ObservableUpDownCounter("process.memory.usage", metric.WithUnit("By"),
		metric.WithDescription("The amount of physical memory in use."))
	if err != nil {
		return err
	}
	fds, err := m.Int64ObservableUpDownCounter("process.unix.file_descriptor.count", metric.WithUnit("{file_descriptor}"),
		metric.WithDescription("Number of unix file descriptors in use by the process."))
	if err != nil {
		return err
	}
	uptime, err := m.Float64ObservableGauge("process.uptime", metric.WithUnit("s"),
		metric.WithDescription("The time the process has been running."))
	if err != nil {
		return err
	}
	gcCount, err := m.Int64ObservableCounter("go.gc.count", metric.WithUnit("{gc_cycle}"),
		metric.WithDescription("Completed garbage collection cycles."))
	if err != nil {
		return err
	}
	gcPause, err := m.Float64ObservableCounter("go.gc.pause.time", metric.WithUnit("s"),
		metric.WithDescription("Total time the garbage collector stopped the world."))
	if err != nil {
		return err
	}
	classes, err := m.Int64ObservableUpDownCounter("go.memory.class", metric.WithUnit("By"),
		metric.WithDescription("Memory the Go runtime has mapped, by runtime/metrics class (go.memory.class)."))
	if err != nil {
		return err
	}
	user := metric.WithAttributes(attribute.String("cpu.mode", "user"))
	system := metric.WithAttributes(attribute.String("cpu.mode", "system"))
	_, err = m.RegisterCallback(func(_ context.Context, o metric.Observer) error {
		if u, s, ok := cpuTime(); ok {
			o.ObserveFloat64(cpu, u, user)
			o.ObserveFloat64(cpu, s, system)
		}
		if n, ok := residentBytes(); ok {
			o.ObserveInt64(rss, n)
		}
		if n, ok := openDescriptors(); ok {
			o.ObserveInt64(fds, n)
		}
		o.ObserveFloat64(uptime, time.Since(started).Seconds())
		var gc debug.GCStats
		debug.ReadGCStats(&gc)
		o.ObserveInt64(gcCount, gc.NumGC)
		o.ObserveFloat64(gcPause, gc.PauseTotal.Seconds())
		samples := make([]rtmetrics.Sample, len(memoryClasses))
		copy(samples, memoryClasses)
		rtmetrics.Read(samples)
		for _, s := range samples {
			class := strings.TrimSuffix(strings.TrimPrefix(s.Name, "/memory/classes/"), ":bytes")
			o.ObserveInt64(classes, int64(s.Value.Uint64()), metric.WithAttributes(attribute.String("go.memory.class", class)))
		}
		return nil
	}, cpu, rss, fds, uptime, gcCount, gcPause, classes)
	return err
}

// residentBytes is the resident set size from /proc/self/statm (Linux).
func residentBytes() (int64, bool) {
	raw, err := os.ReadFile("/proc/self/statm")
	if err != nil {
		return 0, false
	}
	fields := bytes.Fields(raw)
	if len(fields) < 2 {
		return 0, false
	}
	pages, err := strconv.ParseInt(string(fields[1]), 10, 64)
	if err != nil {
		return 0, false
	}
	return pages * int64(os.Getpagesize()), true
}

// openDescriptors counts /proc/self/fd (Linux), less the one the count itself opens.
func openDescriptors() (int64, bool) {
	entries, err := os.ReadDir("/proc/self/fd")
	if err != nil {
		return 0, false
	}
	return int64(len(entries) - 1), true
}
