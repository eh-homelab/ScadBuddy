package activities

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"

	commonpb "go.temporal.io/api/common/v1"
	"go.temporal.io/sdk/converter"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/inventory/store"
)

// TestThePythonInputDecodes reads the payload Python's InventoryCore.write sends for an
// activity's input, as the pydantic converter encodes it (backend
// tests/inventory/test_core_client.py test_the_python_input_reads_in_go writes the file
// and checks it on every run), with the SDK's default converter, which the worker uses.
// The params are a map of strings, the actor and the body raw JSON.
func TestThePythonInputDecodes(t *testing.T) {
	raw, err := os.ReadFile(filepath.Join("testdata", "python_input.json"))
	if err != nil {
		t.Fatal(err)
	}
	var file struct {
		Metadata map[string]string `json:"metadata"`
		Data     string            `json:"data"`
	}
	if err := json.Unmarshal(raw, &file); err != nil {
		t.Fatal(err)
	}
	payload := &commonpb.Payload{Metadata: map[string][]byte{}, Data: []byte(file.Data)}
	for k, v := range file.Metadata {
		payload.Metadata[k] = []byte(v)
	}

	var in Input
	if err := converter.GetDefaultDataConverter().FromPayload(payload, &in); err != nil {
		t.Fatalf("the default converter refuses Python's payload: %v", err)
	}

	if in.Key != "browser:p1:k1" {
		t.Errorf("key = %q", in.Key)
	}
	if got := in.Params["item_id"]; got != "00000000-0000-4000-8000-000000000001" || len(in.Params) != 1 {
		t.Errorf("params = %v", in.Params)
	}
	by, err := store.ReadActor(in.Actor)
	if err != nil {
		t.Fatalf("the actor does not read as store.ReadActor reads it: %v", err)
	}
	if want := `{"kind":"browser","principal":"p1","session":null,"run_id":null}`; string(by) != want {
		t.Errorf("actor = %s, want %s", by, want)
	}
	var use struct {
		Qty    string `json:"qty"`
		LotID  string `json:"lot_id"`
		Reason string `json:"reason"`
	}
	if err := json.Unmarshal(in.Body, &use); err != nil {
		t.Fatal(err)
	}
	if use.Qty != "2.5" || use.LotID != "00000000-0000-4000-8000-000000000002" || use.Reason != "fit a hotend" {
		t.Errorf("body = %+v", use)
	}
}
