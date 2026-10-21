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

/// #3962: every colour Python refuses, the worker refuses too.
#[test]
fn every_refused_colour_is_refused() {
    let doc: Value = serde_json::from_str(VECTORS).unwrap();
    let refused = doc["refused_colours"].as_array().unwrap();
    assert!(!refused.is_empty());
    for colour in refused {
        let mut input = doc["input"][0].clone();
        input["filament_colours"] = Value::Array(vec![colour.clone()]);
        assert!(
            serde_json::from_value::<SlicePlateInput>(input).is_err(),
            "{colour} was taken"
        );
    }
}
