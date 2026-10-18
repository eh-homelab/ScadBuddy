use std::net::TcpListener;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Duration;

fn free_port() -> u16 {
    TcpListener::bind("127.0.0.1:0")
        .unwrap()
        .local_addr()
        .unwrap()
        .port()
}

async fn wait_for_listen(port: u16) {
    for _ in 0..200 {
        if tokio::net::TcpStream::connect(("127.0.0.1", port))
            .await
            .is_ok()
        {
            return;
        }
        tokio::time::sleep(Duration::from_millis(10)).await;
    }
    panic!("nothing listened on {port}");
}

#[tokio::test]
async fn healthz_turns_ok_when_ready_and_metrics_lists_the_stage_histogram() {
    let registry = prometheus::Registry::new();
    let stages = sb_temporal::Stages::register(&registry);
    stages.observe("slice", 1.5);
    let ready = Arc::new(AtomicBool::new(false));
    let port = free_port();
    let _server = sb_temporal::serve_health(port, ready.clone(), registry);
    wait_for_listen(port).await;
    let url = |path: &str| format!("http://127.0.0.1:{port}{path}");
    assert_eq!(reqwest::get(url("/healthz")).await.unwrap().status(), 503);
    ready.store(true, Ordering::SeqCst);
    let ok = reqwest::get(url("/healthz")).await.unwrap();
    assert_eq!(ok.status(), 200);
    assert_eq!(ok.text().await.unwrap(), "ok");
    let body = reqwest::get(url("/metrics"))
        .await
        .unwrap()
        .text()
        .await
        .unwrap();
    assert!(body.contains("scadbuddy_render_stage_seconds_bucket{stage=\"slice\""));
    // The Python histogram's buckets, so the dashboard's quantiles line up.
    assert!(body.contains("scadbuddy_render_stage_seconds_bucket{stage=\"slice\",le=\"900\"} 1"));
    assert!(body.contains("scadbuddy_render_stage_seconds_bucket{stage=\"slice\",le=\"1\"} 0"));
    assert!(body.contains("scadbuddy_render_stage_seconds_bucket{stage=\"slice\",le=\"2\"} 1"));
}

/// The only test in this binary that touches the environment, so nothing reads it
/// while it is written.
#[test]
fn from_env_defaults_the_queue_and_concurrency() {
    let vars = [
        "SCADBUDDY_TEMPORAL_ADDRESS",
        "SCADBUDDY_TEMPORAL_NAMESPACE",
        "SCADBUDDY_TEMPORAL_TASK_QUEUE_SLICER",
        "SCADBUDDY_SLICER_CONCURRENCY",
    ];
    // SAFETY: no other thread in this test binary reads or writes these variables.
    unsafe {
        for v in vars {
            std::env::remove_var(v);
        }
    }
    let from_env = || {
        sb_temporal::WorkerEnv::from_env(
            "SCADBUDDY_TEMPORAL_TASK_QUEUE_SLICER",
            "slicer",
            "SCADBUDDY_SLICER_CONCURRENCY",
        )
    };
    assert!(from_env().is_err(), "the address is required");

    unsafe { std::env::set_var("SCADBUDDY_TEMPORAL_ADDRESS", "temporal:7233") };
    let env = from_env().unwrap();
    assert_eq!(env.address, "temporal:7233");
    assert_eq!(env.namespace, "default");
    assert_eq!(env.task_queue, "slicer");
    assert!(env.concurrency >= 1);
    assert_eq!(env.metrics_port, 9090);

    unsafe {
        std::env::set_var("SCADBUDDY_TEMPORAL_NAMESPACE", "scadbuddy");
        std::env::set_var("SCADBUDDY_TEMPORAL_TASK_QUEUE_SLICER", "slicer-blue");
        std::env::set_var("SCADBUDDY_SLICER_CONCURRENCY", "3");
    }
    let env = from_env().unwrap();
    assert_eq!(
        (
            env.namespace.as_str(),
            env.task_queue.as_str(),
            env.concurrency
        ),
        ("scadbuddy", "slicer-blue", 3)
    );

    for bad in ["0", "two", ""] {
        unsafe { std::env::set_var("SCADBUDDY_SLICER_CONCURRENCY", bad) };
        if bad.is_empty() {
            assert!(from_env().unwrap().concurrency >= 1, "empty is the default");
        } else {
            assert!(from_env().is_err(), "{bad:?} is refused");
        }
    }
    unsafe {
        for v in vars {
            std::env::remove_var(v);
        }
    }
}
