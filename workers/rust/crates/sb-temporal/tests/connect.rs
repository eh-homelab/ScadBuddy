//! Apart from `health.rs`, whose environment test writes variables: a connection
//! may read the environment through libc, which `set_var` would race.

use std::net::TcpListener;
use std::time::Duration;

fn free_port() -> u16 {
    TcpListener::bind("127.0.0.1:0")
        .unwrap()
        .local_addr()
        .unwrap()
        .port()
}

#[tokio::test]
async fn connect_fails_instead_of_hanging_when_nothing_listens() {
    let env = sb_temporal::WorkerEnv {
        address: format!("127.0.0.1:{}", free_port()),
        namespace: "default".into(),
        task_queue: "slicer".into(),
        concurrency: 1,
        metrics_port: 9090,
    };
    let runtime = temporalio_sdk::Runtime::from_current_tokio(Default::default()).unwrap();
    let started = std::time::Instant::now();
    assert!(sb_temporal::connect(&env, &runtime).await.is_err());
    assert!(started.elapsed() < Duration::from_secs(30));
}
