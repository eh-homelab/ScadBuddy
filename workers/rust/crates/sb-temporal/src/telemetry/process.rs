//! The process's own metrics, with the semantic conventions' names (as the Go services'
//! `tools/go/telemetry`): `process.cpu.time` by `cpu.mode`, `process.memory.usage` (the
//! resident set), `process.unix.file_descriptor.count` and `process.uptime`. A value the
//! platform does not give (no `/proc`) is not observed.

use std::time::Instant;

use nix::sys::resource::{UsageWho, getrusage};
use opentelemetry::KeyValue;
use opentelemetry::metrics::{
    MeterProvider as _, ObservableCounter, ObservableGauge, ObservableUpDownCounter,
};
use opentelemetry_sdk::metrics::SdkMeterProvider;

/// The instruments, kept for as long as the provider exports them.
pub struct ProcessMetrics {
    _cpu: ObservableCounter<f64>,
    _memory: ObservableUpDownCounter<i64>,
    _descriptors: ObservableUpDownCounter<i64>,
    _uptime: ObservableGauge<f64>,
}

impl ProcessMetrics {
    /// Registers the instruments on `provider`; each is read at every collection.
    pub fn register(provider: &SdkMeterProvider) -> Self {
        let meter = provider.meter("sb_temporal::process");
        let started = Instant::now();
        let user = [KeyValue::new("cpu.mode", "user")];
        let system = [KeyValue::new("cpu.mode", "system")];
        Self {
            _cpu: meter
                .f64_observable_counter("process.cpu.time")
                .with_unit("s")
                .with_description("Total CPU seconds broken down by different CPU modes.")
                .with_callback(move |o| {
                    if let Some((u, s)) = cpu_time() {
                        o.observe(u, &user);
                        o.observe(s, &system);
                    }
                })
                .build(),
            _memory: meter
                .i64_observable_up_down_counter("process.memory.usage")
                .with_unit("By")
                .with_description("The amount of physical memory in use.")
                .with_callback(|o| {
                    if let Some(n) = resident_bytes() {
                        o.observe(n, &[]);
                    }
                })
                .build(),
            _descriptors: meter
                .i64_observable_up_down_counter("process.unix.file_descriptor.count")
                .with_unit("{file_descriptor}")
                .with_description("Number of unix file descriptors in use by the process.")
                .with_callback(|o| {
                    if let Some(n) = open_descriptors() {
                        o.observe(n, &[]);
                    }
                })
                .build(),
            _uptime: meter
                .f64_observable_gauge("process.uptime")
                .with_unit("s")
                .with_description("The time the process has been running.")
                .with_callback(move |o| o.observe(started.elapsed().as_secs_f64(), &[]))
                .build(),
        }
    }
}

/// The process's user and system CPU seconds.
fn cpu_time() -> Option<(f64, f64)> {
    let usage = getrusage(UsageWho::RUSAGE_SELF).ok()?;
    let seconds = |t: nix::sys::time::TimeVal| t.tv_sec() as f64 + t.tv_usec() as f64 / 1e6;
    Some((seconds(usage.user_time()), seconds(usage.system_time())))
}

/// The resident set size from `/proc/self/statm` (Linux): its second field, in pages.
fn resident_bytes() -> Option<i64> {
    let statm = std::fs::read_to_string("/proc/self/statm").ok()?;
    let pages: i64 = statm.split_whitespace().nth(1)?.parse().ok()?;
    let page = nix::unistd::sysconf(nix::unistd::SysconfVar::PAGE_SIZE).ok()??;
    Some(pages * page)
}

/// The entries of `/proc/self/fd` (Linux), less the one the count itself opens.
fn open_descriptors() -> Option<i64> {
    let count = std::fs::read_dir("/proc/self/fd").ok()?.count();
    Some(count as i64 - 1)
}
