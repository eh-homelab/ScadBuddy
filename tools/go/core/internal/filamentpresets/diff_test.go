package filamentpresets

import (
	"fmt"
	"net/http"
	"testing"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/jsonv"
)

func TestDiffDefaultsToPreviousAndCurrent(t *testing.T) {
	e := newEnv(t)
	id := e.preset("3dfp", "PLA", `{"name":"PLA","a":"1"}`, `{"name":"PLA","a":"2","b":"x"}`,
		`{"name":"PLA","a":"3","printhost_apikey":"k"}`)
	status, cache, body := e.get(fmt.Sprintf("%s/%d/diff", listPath, id))
	if status != http.StatusOK || cache != "no-store" {
		t.Fatalf("%d %q %s", status, cache, jsonv.Dumps(body))
	}
	want := fmt.Sprintf(`{"preset_id":%d,"from_version":2,"to_version":3,"changes":[`+
		`{"key":"a","before":"2","after":"3"},{"key":"b","before":"x","after":null}]}`, id)
	if got := jsonv.Dumps(body); got != want {
		t.Errorf("\n got %s\nwant %s", got, want)
	}
	_, _, body = e.get(fmt.Sprintf("%s/%d/diff?from_version=1&to_version=2", listPath, id))
	if got := jsonv.Dumps(field(body, "changes")); got != `[{"key":"a","before":"1","after":"2"},{"key":"b","before":null,"after":"x"}]` {
		t.Errorf("1 to 2: %s", got)
	}
	_, _, body = e.get(fmt.Sprintf("%s/%d/diff?to_version=2", listPath, id))
	if number(body, "from_version") != "1" || number(body, "to_version") != "2" {
		t.Errorf("to 2: %s", jsonv.Dumps(body))
	}

	one := e.preset("manual", "One", `{"name":"One"}`)
	_, _, body = e.get(fmt.Sprintf("%s/%d/diff", listPath, one))
	if got := jsonv.Dumps(body); got != fmt.Sprintf(`{"preset_id":%d,"from_version":1,"to_version":1,"changes":[]}`, one) {
		t.Errorf("one version: %s", got)
	}
}

func TestDiffRefusesAnUnknownVersionAndALostBlob(t *testing.T) {
	e := newEnv(t)
	first := `{"name":"PLA","a":"1"}`
	id := e.preset("manual", "PLA", first, `{"name":"PLA","a":"2"}`)
	status, _, body := e.get(fmt.Sprintf("%s/%d/diff?to_version=9", listPath, id))
	if status != http.StatusNotFound || field(body, "code") != "version_not_found" {
		t.Errorf("to 9: %d %s", status, jsonv.Dumps(body))
	}
	for _, q := range []string{"from_version=0", "to_version=x", "from_version=-1", "to_version="} {
		status, _, body := e.get(fmt.Sprintf("%s/%d/diff?%s", listPath, id, q))
		if status != http.StatusUnprocessableEntity {
			t.Errorf("%s: %d %s", q, status, jsonv.Dumps(body))
		}
	}
	key, _ := keyOf(first)
	e.blobs.drop(key)
	status, _, body = e.get(fmt.Sprintf("%s/%d/diff", listPath, id))
	if status != http.StatusConflict || field(body, "code") != "document_missing" {
		t.Errorf("lost v1: %d %s", status, jsonv.Dumps(body))
	}
	status, _, body = e.get(listPath + "/9999/diff")
	if status != http.StatusNotFound || field(body, "code") != "preset_not_found" {
		t.Errorf("unknown id: %d %s", status, jsonv.Dumps(body))
	}
}
