mod support;

use std::sync::{Arc, Mutex};

use chrono::{Duration, Utc};
use sb_slicer::activity::{SLICER_ERROR, SlicerActivities};
use sb_slicer::models::SliceProgress;
use sha2::{Digest, Sha256};
use support::{activities, input, serve, url};
use temporalio_sdk::activities::ActivityError;
use temporalio_sdk::testing::{ActivityEnvironment, ActivityEnvironmentError};

fn sha(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}

fn application(err: ActivityEnvironmentError) -> (Option<String>, bool) {
    match err {
        ActivityEnvironmentError::Activity(ActivityError::Application(f)) => {
            (f.type_name().map(str::to_owned), f.is_non_retryable())
        }
        other => panic!("expected an application failure, got {other:?}"),
    }
}

#[tokio::test]
async fn a_slice_puts_its_output_and_heartbeats_progress() {
    let (base, seen) = serve().await;
    let beats: Arc<Mutex<Vec<SliceProgress>>> = Arc::default();
    let env = ActivityEnvironment::builder()
        .register_activities(activities(""))
        .on_heartbeat({
            let beats = beats.clone();
            move |details| {
                let p = details.downcast::<SliceProgress>().expect("SliceProgress");
                beats.lock().unwrap().push(*p);
            }
        })
        .build();
    let out = env
        .run(SlicerActivities::slice_plate, input(&base))
        .await
        .unwrap();

    let seen = seen.lock().unwrap();
    let body = &seen.puts["sliced"];
    assert_eq!(out.sliced_sha256, sha(body));
    assert_eq!(out.sliced_size, body.len() as u64);
    assert_eq!(body.as_slice(), b"PK fake 3mf");
    assert_eq!(out.exit_code, 0);
    assert_eq!(out.warnings, vec!["thin wall".to_owned()]);
    assert!(out.stderr_tail.contains("fake stderr line"));
    assert_eq!(out.slicedata_sha256, None);
    assert_eq!(seen.gets, vec!["input", "machine", "process", "f1", "f2"]);
    assert_eq!(beats.lock().unwrap().last().unwrap().total_percent, 100);
}

#[tokio::test]
async fn slicedata_is_put_as_a_zip_when_asked() {
    let (base, seen) = serve().await;
    let env = ActivityEnvironment::builder()
        .register_activities(activities(""))
        .build();
    let mut req = input(&base);
    req.slicedata = Some(url(&base, "/sink/slicedata"));
    let out = env.run(SlicerActivities::slice_plate, req).await.unwrap();

    let seen = seen.lock().unwrap();
    let zip = &seen.puts["slicedata"];
    assert_eq!(out.slicedata_sha256.as_deref(), Some(sha(zip).as_str()));
    assert_eq!(out.slicedata_size, Some(zip.len() as u64));
    assert!(zip.starts_with(b"PK"));
}

#[tokio::test]
async fn a_validation_failure_is_not_retried() {
    let (base, seen) = serve().await;
    let env = ActivityEnvironment::builder()
        .register_activities(activities("exit-192"))
        .build();
    let err = env
        .run(SlicerActivities::slice_plate, input(&base))
        .await
        .unwrap_err();
    assert_eq!(application(err), (Some(SLICER_ERROR.to_owned()), true));
    assert!(seen.lock().unwrap().puts.is_empty());
}

#[tokio::test]
async fn any_other_failure_is_retried() {
    let (base, _seen) = serve().await;
    let env = ActivityEnvironment::builder()
        .register_activities(activities("exit-1"))
        .build();
    let err = env
        .run(SlicerActivities::slice_plate, input(&base))
        .await
        .unwrap_err();
    assert_eq!(application(err), (Some(SLICER_ERROR.to_owned()), false));
}

#[tokio::test]
async fn an_expired_input_is_refused_without_a_request() {
    let (base, seen) = serve().await;
    let env = ActivityEnvironment::builder()
        .register_activities(activities(""))
        .build();
    let mut req = input(&base);
    req.input_3mf.expires_at = Utc::now() - Duration::seconds(1);
    let err = env
        .run(SlicerActivities::slice_plate, req)
        .await
        .unwrap_err();
    assert_eq!(application(err), (Some("TransferRefused".to_owned()), true));
    assert!(seen.lock().unwrap().gets.is_empty());
}

#[tokio::test]
async fn a_cancelled_activity_is_cancelled() {
    let (base, _seen) = serve().await;
    let env = ActivityEnvironment::builder()
        .register_activities(activities("hang"))
        .build();
    let env = Arc::new(env);
    let canceller = env.clone();
    tokio::spawn(async move {
        tokio::time::sleep(std::time::Duration::from_millis(300)).await;
        canceller.cancel();
    });
    let err = tokio::time::timeout(
        std::time::Duration::from_secs(5),
        env.run(SlicerActivities::slice_plate, input(&base)),
    )
    .await
    .expect("returns within 5 s")
    .unwrap_err();
    assert!(matches!(
        err,
        ActivityEnvironmentError::Activity(ActivityError::Cancelled { .. })
    ));
}
