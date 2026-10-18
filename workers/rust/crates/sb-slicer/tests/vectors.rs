use sb_slicer::models::{SlicePlateInput, SlicePlateOutput, SliceProgress};
use serde_json::Value;

const VECTORS: &str = include_str!("../../../vectors/slice_plate.json");

fn round_trip<T: serde::de::DeserializeOwned + serde::Serialize>(raw: &Value) {
    let parsed: T = serde_json::from_value(raw.clone()).expect("parses");
    assert_eq!(
        serde_json::to_value(&parsed).unwrap(),
        *raw,
        "a field was lost, renamed or reformatted"
    );
}

#[test]
fn every_vector_round_trips() {
    let doc: Value = serde_json::from_str(VECTORS).unwrap();
    for raw in doc["input"].as_array().unwrap() {
        round_trip::<SlicePlateInput>(raw);
    }
    for raw in doc["progress"].as_array().unwrap() {
        round_trip::<SliceProgress>(raw);
    }
    for raw in doc["output"].as_array().unwrap() {
        round_trip::<SlicePlateOutput>(raw);
    }
}
