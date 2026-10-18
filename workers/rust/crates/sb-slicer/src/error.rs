//! How a failed `slice_plate` reaches Temporal: each failure's type and whether a retry
//! can help.

use sb_transfer::TransferError;
use serde_json::json;
use temporalio_common::error::ApplicationFailure;
use temporalio_sdk::activities::ActivityError;

use crate::activity::{SLICER_ERROR, SLICER_WORKER_ERROR, VALIDATION_EXIT};

/// A URL that will never work: the caller mints new ones on its next start.
pub(crate) const TRANSFER_REFUSED: &str = "TransferRefused";
/// A transfer worth another attempt.
pub(crate) const TRANSFER_ERROR: &str = "TransferError";

fn failure(
    message: String,
    type_name: &str,
    non_retryable: bool,
    details: Option<serde_json::Value>,
) -> ActivityError {
    let failure = ApplicationFailure::builder(message)
        .type_name(type_name.to_owned())
        .non_retryable(non_retryable)
        .maybe_details(details)
        .build();
    ActivityError::application(failure)
}

pub(crate) fn transfer(e: TransferError) -> ActivityError {
    let refused = match &e {
        TransferError::Missing
        | TransferError::Expired
        | TransferError::Refused(_)
        | TransferError::InvalidHeader(_) => true,
        TransferError::Status(code) => !(*code >= 500 || *code == 429),
        TransferError::Io(_) | TransferError::Http(_) => false,
    };
    let type_name = if refused {
        TRANSFER_REFUSED
    } else {
        TRANSFER_ERROR
    };
    failure(e.to_string(), type_name, refused, None)
}

/// The CLI exited non-zero: 192 is a validation failure, which no retry changes.
pub(crate) fn slicer(exit_code: i32, stderr_tail: &str) -> ActivityError {
    failure(
        format!("the slicer exited with code {exit_code}"),
        SLICER_ERROR,
        exit_code == VALIDATION_EXIT,
        Some(json!({"exit_code": exit_code, "stderr_tail": stderr_tail})),
    )
}

/// The CLI exited 0 without the sliced 3MF (a plate with nothing to slice): the same
/// input does the same again, so no retry helps.
pub(crate) fn no_output(stderr_tail: &str) -> ActivityError {
    failure(
        "the slicer exited 0 without the sliced 3MF".to_owned(),
        SLICER_ERROR,
        true,
        Some(json!({"exit_code": 0, "stderr_tail": stderr_tail})),
    )
}

/// The worker's own failure (a temp dir, the fifo, starting the CLI): retryable, under a
/// type of its own so a `SlicerError` always has its details.
pub(crate) fn local(e: impl std::fmt::Display) -> ActivityError {
    failure(e.to_string(), SLICER_WORKER_ERROR, false, None)
}

#[cfg(test)]
mod tests {
    use temporalio_common::data_converters::{DataConverter, SerializationContextData};
    use temporalio_common::error::{OutgoingActivityError, OutgoingError};
    use temporalio_common::protos::temporal::api::failure::v1::failure::FailureInfo;

    use super::*;

    /// `e` as a caller receives it: its type, retryability and details decoded as JSON,
    /// encoded the way the SDK completes an activity.
    fn on_the_wire(e: ActivityError) -> (String, bool, Option<serde_json::Value>) {
        let ActivityError::Application(f) = e else {
            panic!("expected an application failure, got {e:?}");
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

    fn kind(e: TransferError) -> (String, bool) {
        match transfer(e) {
            ActivityError::Application(f) => {
                (f.type_name().unwrap().to_owned(), f.is_non_retryable())
            }
            other => panic!("expected an application failure, got {other:?}"),
        }
    }

    fn refused() -> (String, bool) {
        (TRANSFER_REFUSED.to_owned(), true)
    }

    fn retryable() -> (String, bool) {
        (TRANSFER_ERROR.to_owned(), false)
    }

    #[test]
    fn the_workers_own_failures_are_retried_without_details() {
        assert_eq!(
            on_the_wire(local("no space left on device")),
            (SLICER_WORKER_ERROR.to_owned(), false, None)
        );
    }

    #[test]
    fn a_slicer_error_always_carries_its_exit_and_stderr() {
        let details = json!({"exit_code": 1, "stderr_tail": "boom"});
        assert_eq!(
            on_the_wire(slicer(1, "boom")),
            (SLICER_ERROR.to_owned(), false, Some(details))
        );
        let details = json!({"exit_code": 192, "stderr_tail": ""});
        assert_eq!(
            on_the_wire(slicer(VALIDATION_EXIT, "")),
            (SLICER_ERROR.to_owned(), true, Some(details))
        );
        let details = json!({"exit_code": 0, "stderr_tail": "empty plate"});
        assert_eq!(
            on_the_wire(no_output("empty plate")),
            (SLICER_ERROR.to_owned(), true, Some(details))
        );
    }

    #[test]
    fn refused_urls_are_not_retried() {
        assert_eq!(kind(TransferError::Refused(403)), refused());
        assert_eq!(kind(TransferError::Refused(401)), refused());
        assert_eq!(kind(TransferError::Missing), refused());
        assert_eq!(kind(TransferError::Expired), refused());
        assert_eq!(kind(TransferError::Status(302)), refused());
        assert_eq!(kind(TransferError::Status(400)), refused());
        assert_eq!(
            kind(TransferError::InvalidHeader("bad name".into())),
            refused()
        );
    }

    #[test]
    fn transient_failures_are_retried() {
        assert_eq!(kind(TransferError::Status(429)), retryable());
        assert_eq!(kind(TransferError::Status(500)), retryable());
        assert_eq!(kind(TransferError::Status(503)), retryable());
        assert_eq!(
            kind(TransferError::Io(std::io::Error::other("reset"))),
            retryable()
        );
        assert_eq!(kind(TransferError::Http("timed out".into())), retryable());
    }
}
