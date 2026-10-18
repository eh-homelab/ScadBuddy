use prometheus::{HistogramOpts, HistogramVec, Registry};

/// The render histograms' buckets, the same as the Python worker's so the
/// `scadbuddy-renders` dashboard's quantiles read both alike.
// backend/scadbuddy/core/metrics.py:68
// RENDER_BUCKETS = (0.25, 0.5, 1, 2, 5, 10, 20, 30, 60, 120, 240, 480, 900)
pub const RENDER_BUCKETS: &[f64] = &[
    0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 20.0, 30.0, 60.0, 120.0, 240.0, 480.0, 900.0,
];

/// `scadbuddy_render_stage_seconds{stage}`, the histogram the Python render worker emits
/// under the same name and help (`core/metrics.py` `stage_duration`).
#[derive(Clone)]
pub struct Stages {
    pub stage_seconds: HistogramVec,
}

impl Stages {
    /// Registers the histogram on `registry`.
    ///
    /// # Panics
    /// If `registry` already holds a collector of that name: registering twice is a bug.
    pub fn register(registry: &Registry) -> Self {
        let stage_seconds = HistogramVec::new(
            HistogramOpts::new(
                "scadbuddy_render_stage_seconds",
                "Time spent in each step of a render.",
            )
            .buckets(RENDER_BUCKETS.to_vec()),
            &["stage"],
        )
        .expect("the stage histogram's options are valid");
        registry
            .register(Box::new(stage_seconds.clone()))
            .expect("scadbuddy_render_stage_seconds is registered once");
        Self { stage_seconds }
    }

    /// Records one run of `stage`. A stage is a fixed name in code, never a value.
    pub fn observe(&self, stage: &str, seconds: f64) {
        self.stage_seconds
            .with_label_values(&[stage])
            .observe(seconds);
    }
}
