//! How a failed `slice_plate` reaches Temporal: each failure's type and whether a retry
//! can help.

use sb_transfer::TransferError;
use serde_json::json;
use temporalio_common::error::ApplicationFailure;
use temporalio_sdk::activities::ActivityError;

use crate::activity::{SLICER_ERROR, VALIDATION_EXIT};

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
        TransferError::Missing | TransferError::Expired | TransferError::Refused(_) => true,
        TransferError::Status(code) => !(*code >= 500 || *code == 429),
        TransferError::Digest { .. } | TransferError::Io(_) | TransferError::Http(_) => false,
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

/// The worker's own failure (a temp dir, the fifo, starting the CLI): retryable.
pub(crate) fn local(e: impl std::fmt::Display) -> ActivityError {
    failure(e.to_string(), SLICER_ERROR, false, None)
}
