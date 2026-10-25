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

/// The failure as a caller receives it: encoded the way the SDK completes an activity,
/// its details decoded as JSON.
fn details(err: ActivityEnvironmentError) -> (String, bool, Option<serde_json::Value>) {
    use temporalio_common::data_converters::{DataConverter, SerializationContextData};
    use temporalio_common::error::{OutgoingActivityError, OutgoingError};
    use temporalio_common::protos::temporal::api::failure::v1::failure::FailureInfo;
    let ActivityEnvironmentError::Activity(ActivityError::Application(f)) = err else {
        panic!("expected an application failure, got {err:?}");
    };
    let failure = DataConverter::default().to_failure(
        &SerializationContextData::None,
        OutgoingError::Activity(OutgoingActivityError::Application(f)),
    );
    let Some(FailureInfo::ApplicationFailureInfo(info)) = failure.failure_info else {
        panic!("no application failure info");
    };
    let details = info
        .details
        .map(|d| serde_json::from_slice(&d.payloads[0].data).expect("JSON details"));
    (info.r#type, info.non_retryable, details)
}

#[tokio::test]
async fn an_exit_0_without_output_is_a_non_retryable_slicer_error_before_any_put() {
    let (base, seen) = serve().await;
    let env = ActivityEnvironment::builder()
        .register_activities(activities("no-output"))
        .build();
    let err = env
        .run(SlicerActivities::slice_plate, input(&base))
        .await
        .unwrap_err();
    let (type_name, non_retryable, details) = details(err);
    assert_eq!(type_name, SLICER_ERROR);
    assert!(non_retryable, "the same input exports nothing again");
    let details = details.expect("details");
    assert_eq!(details["exit_code"], 0);
    let tail = details["stderr_tail"].as_str().expect("a stderr tail");
    assert!(tail.contains("fake stderr line"), "{tail}");
    assert!(seen.lock().unwrap().puts.is_empty());
}

#[tokio::test]
async fn an_empty_plate_says_why_from_the_clis_log() {
    // The CLI writes an empty plate's reason to stdout and nothing to stderr (#4638).
    let (base, _seen) = serve().await;
    let env = ActivityEnvironment::builder()
        .register_activities(activities("empty-plate"))
        .build();
    let err = env
        .run(SlicerActivities::slice_plate, input(&base))
        .await
        .unwrap_err();
    let (type_name, _, details) = details(err);
    assert_eq!(type_name, SLICER_ERROR);
    let details = details.expect("details");
    assert_eq!(details["exit_code"], 206);
    assert_eq!(details["stderr_tail"], "");
    assert_eq!(
        details["errors"],
        "plate 2: Nothing to be sliced, Either the print is empty or no object is fully \
         inside the print volume before apply."
    );
}

#[tokio::test]
async fn a_failed_slice_still_records_its_stage_time() {
    let (base, _seen) = serve().await;
    let acts = activities("exit-1");
    let stages = acts.stages.clone();
    let env = ActivityEnvironment::builder()
        .register_activities(acts)
        .build();
    env.run(SlicerActivities::slice_plate, input(&base))
        .await
        .unwrap_err();
    let slice = stages.stage_seconds.with_label_values(&["slice"]);
    assert_eq!(slice.get_sample_count(), 1);
}

/// The keepalive runs for the whole activity, not only while the CLI does: a stalled GET
/// repeats the zeroed progress and a stalled PUT the last one, each at least every
/// `keepalive`.
#[tokio::test]
async fn stalled_transfers_keep_heartbeating() {
    let (base, seen) = serve().await;
    seen.lock().unwrap().stall = std::time::Duration::from_millis(800);
    let beats: Arc<Mutex<Vec<SliceProgress>>> = Arc::default();
    let mut acts = activities("");
    acts.keepalive = std::time::Duration::from_millis(100);
    let env = ActivityEnvironment::builder()
        .register_activities(acts)
        .on_heartbeat({
            let beats = beats.clone();
            move |details| {
                let p = details.downcast::<SliceProgress>().expect("SliceProgress");
                beats.lock().unwrap().push(*p);
            }
        })
        .build();
    let mut req = input(&base);
    req.input_3mf = url(&base, "/stall/input");
    req.sliced_3mf = url(&base, "/stall-sink/sliced");
    env.run(SlicerActivities::slice_plate, req).await.unwrap();

    let beats = beats.lock().unwrap();
    let before_cli = beats
        .iter()
        .take_while(|p| p.plate_count == 0 && p.total_percent == 0)
        .count();
    assert!(
        before_cli >= 4,
        "{before_cli} beats during the GET: {beats:?}"
    );
    let done = beats
        .iter()
        .position(|p| p.total_percent == 100)
        .expect("a 100% beat");
    let after_cli = beats[done..]
        .iter()
        .filter(|p| p.total_percent == 100)
        .count();
    assert!(after_cli >= 5, "{after_cli} beats at 100%: {beats:?}");
}
