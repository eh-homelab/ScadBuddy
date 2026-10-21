//! The `slice_plate` payloads, field for field the pydantic models in
//! `backend/scadbuddy/slicing/models.py`; `vectors/slice_plate.json` pins the two.
//! No `deny_unknown_fields` (Python may add fields), and every field is serialised
//! because Python dumps every field.

pub use sb_transfer::TransferUrl;
use serde::de::Error as _;
use serde::{Deserialize, Deserializer, Serialize};

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Arrange {
    #[default]
    No,
    Yes,
    Auto,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SlicePlateInput {
    pub input_3mf: TransferUrl,
    /// 0 = every plate.
    pub plate: u32,
    pub machine_preset: TransferUrl,
    pub process_preset: TransferUrl,
    /// In slot order.
    pub filament_presets: Vec<TransferUrl>,
    pub sliced_3mf: TransferUrl,
    #[serde(default)]
    pub slicedata: Option<TransferUrl>,
    #[serde(default)]
    pub arrange: Arrange,
    #[serde(default = "default_allow_newer_file")]
    pub allow_newer_file: bool,
    #[serde(default = "default_debug")]
    pub debug: u8,
    /// Each slot's colour, in slot order: `--filament-colour` (#3962). Joined with `;`,
    /// so a colour holding one is refused here, whoever sent it.
    #[serde(default, deserialize_with = "slot_colours")]
    pub filament_colours: Vec<String>,
}

fn slot_colours<'de, D: Deserializer<'de>>(deserializer: D) -> Result<Vec<String>, D::Error> {
    let colours = Vec::<String>::deserialize(deserializer)?;
    if colours.iter().any(|colour| colour.contains(';')) {
        return Err(D::Error::custom("a slot colour holds a ';'"));
    }
    Ok(colours)
}

fn default_allow_newer_file() -> bool {
    true
}

fn default_debug() -> u8 {
    1
}

/// One heartbeat's details.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct SliceProgress {
    pub plate_index: u32,
    pub plate_count: u32,
    pub plate_percent: u32,
    pub total_percent: u32,
    #[serde(default)]
    pub message: String,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct SlicePlateOutput {
    pub sliced_sha256: String,
    pub sliced_size: u64,
    #[serde(default)]
    pub slicedata_sha256: Option<String>,
    #[serde(default)]
    pub slicedata_size: Option<u64>,
    #[serde(default)]
    pub warnings: Vec<String>,
    pub exit_code: i32,
    #[serde(default)]
    pub stderr_tail: String,
    pub elapsed_seconds: f64,
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn url() -> serde_json::Value {
        json!({"url": "u", "expires_at": "2026-10-16T12:00:00.123456+02:00"})
    }

    #[test]
    fn expires_at_dumps_as_pydantic_does() {
        let t: TransferUrl = serde_json::from_value(url()).unwrap();
        assert_eq!(
            serde_json::to_value(&t).unwrap()["expires_at"],
            "2026-10-16T10:00:00.123456Z"
        );
    }

    #[test]
    fn a_colour_holding_the_separator_is_refused() {
        let input = |colour: &str| {
            serde_json::from_value::<SlicePlateInput>(json!({
                "input_3mf": url(), "plate": 0, "machine_preset": url(), "process_preset": url(),
                "filament_presets": [], "sliced_3mf": url(), "filament_colours": [colour],
            }))
        };
        assert!(input("#FF0000;#00FF00").is_err());
        assert_eq!(input("#FF0000").unwrap().filament_colours, ["#FF0000"]);
    }

    #[test]
    fn omitted_fields_take_pythons_defaults() {
        let input: SlicePlateInput = serde_json::from_value(json!({
            "input_3mf": url(), "plate": 0, "machine_preset": url(), "process_preset": url(),
            "filament_presets": [], "sliced_3mf": url(),
        }))
        .unwrap();
        assert!(input.input_3mf.headers.is_empty());
        assert_eq!(input.slicedata, None);
        assert_eq!(input.arrange, Arrange::No);
        assert!(input.allow_newer_file);
        assert_eq!(input.debug, 1);
        assert!(input.filament_colours.is_empty());

        let progress: SliceProgress = serde_json::from_value(json!({
            "plate_index": 0, "plate_count": 1, "plate_percent": 0, "total_percent": 0,
        }))
        .unwrap();
        assert_eq!(progress.message, "");

        let output: SlicePlateOutput = serde_json::from_value(json!({
            "sliced_sha256": "a", "sliced_size": 1, "exit_code": 0, "elapsed_seconds": 1.0,
        }))
        .unwrap();
        assert_eq!(output.slicedata_sha256, None);
        assert_eq!(output.slicedata_size, None);
        assert!(output.warnings.is_empty());
        assert_eq!(output.stderr_tail, "");
    }
}
