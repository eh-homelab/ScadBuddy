package filamentpresets

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"os"
	"path/filepath"
	"runtime"
	"slices"
	"strings"
	"testing"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/blobstore"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/jsonv"
)

func TestReadDocumentGivesMemberFile(t *testing.T) {
	f := newFake()
	f.put("k", blob{body: packed(t, map[string]string{"file": `{"z":"1","a":["2"]}`, "other": `{}`})})
	doc, code, err := readDocument(context.Background(), f, "k")
	if err != nil || code != "" || jsonv.Dumps(doc) != `{"z":"1","a":["2"]}` {
		t.Fatalf("%s %q %v", jsonv.Dumps(doc), code, err)
	}
}

func TestReadDocumentMapsEachError(t *testing.T) {
	good := packed(t, map[string]string{"file": `{"a":"1"}`})
	for _, c := range []struct {
		name string
		blob *blob
		want string
	}{
		{"no row or object", nil, "document_missing"},
		{"no member file", &blob{body: packed(t, map[string]string{"other": `{}`})}, "document_missing"},
		{"over the cap", &blob{body: good, size: DocumentLimit + 1}, "document_too_large"},
		{"a member over the cap", &blob{body: packed(t, map[string]string{"file": `{"a":"` + strings.Repeat("x", DocumentLimit) + `"}`})}, "document_too_large"},
		{"the wrong bytes", &blob{body: good, readErr: fmt.Errorf("%w: x", blobstore.ErrBlobMismatch)}, "document_unreadable"},
		{"no zip", &blob{body: []byte("not a zip")}, "document_unreadable"},
		{"no JSON", &blob{body: packed(t, map[string]string{"file": `{"a":`})}, "document_unreadable"},
		{"JSON that is no object", &blob{body: packed(t, map[string]string{"file": `["a"]`})}, "document_unreadable"},
		{"no S3 store", &blob{openErr: blobstore.ErrNotConfigured}, "store_not_configured"},
		{"S3 fails", &blob{openErr: errors.New("blobstore: the S3 store answered 500")}, "store_unavailable"},
		{"the stream fails", &blob{body: good, readErr: errors.New("blobstore: reading the blob: reset")}, "store_unavailable"},
	} {
		f := newFake()
		if c.blob != nil {
			f.put("k", *c.blob)
		}
		doc, code, _ := readDocument(context.Background(), f, "k")
		if code != c.want || doc != nil {
			t.Errorf("%s: %q %s, want %q", c.name, code, jsonv.Dumps(doc), c.want)
		}
	}
}

func TestReadDocumentOverTheCapSendsNoGet(t *testing.T) {
	f := newFake()
	f.put("k", blob{body: packed(t, map[string]string{"file": `{}`}), size: DocumentLimit + 1})
	if _, code, _ := readDocument(context.Background(), f, "k"); code != "document_too_large" {
		t.Fatal(code)
	}
	if len(f.opened) != 0 {
		t.Fatalf("GETs %v", f.opened)
	}
}

func keys(v any) []string {
	var out []string
	for _, m := range v.(jsonv.Object) {
		out = append(out, m.Key)
	}
	return out
}

func TestDetailListsVersionsNewestFirstWithTheCurrentDocument(t *testing.T) {
	e := newEnv(t)
	id := e.preset("3dfp", "PLA", `{"name":"PLA","v":"1"}`, `{"name":"PLA","v":"2"}`,
		`{"z":"3","name":"PLA","printhost_apikey":"k","a":{"api_token":"t","b":"1"}}`)
	status, cache, body := e.get(fmt.Sprintf("%s/%d", listPath, id))
	if status != http.StatusOK || cache != "no-store" {
		t.Fatalf("%d %q %s", status, cache, jsonv.Dumps(body))
	}
	var versions []string
	for _, v := range field(body, "versions").([]any) {
		versions = append(versions, number(v, "version"))
	}
	if !slices.Equal(versions, []string{"3", "2", "1"}) {
		t.Errorf("versions %v", versions)
	}
	if got := jsonv.Dumps(field(body, "document")); got != `{"z":"3","name":"PLA","a":{"b":"1"}}` {
		t.Errorf("document %s", got)
	}
	if field(body, "document_error") != nil || number(body, "version") != "3" {
		t.Errorf("%s", jsonv.Dumps(body))
	}
}

func TestDetailOfALostBlobHasNoDocument(t *testing.T) {
	for want, change := range map[string]func(f *fakeBlobs, key string){
		"document_missing":     func(f *fakeBlobs, key string) { f.drop(key) },
		"document_too_large":   func(f *fakeBlobs, key string) { f.put(key, blob{body: []byte("x"), size: DocumentLimit + 1}) },
		"document_unreadable":  func(f *fakeBlobs, key string) { f.put(key, blob{body: []byte("not a zip")}) },
		"store_not_configured": func(f *fakeBlobs, key string) { f.put(key, blob{openErr: blobstore.ErrNotConfigured}) },
		"store_unavailable":    func(f *fakeBlobs, key string) { f.put(key, blob{openErr: errors.New("blobstore: down")}) },
	} {
		e := newEnv(t)
		doc := `{"name":"PLA"}`
		id := e.preset("manual", "PLA", doc)
		key, _ := keyOf(doc)
		change(e.blobs, key)
		status, _, body := e.get(fmt.Sprintf("%s/%d", listPath, id))
		if status != http.StatusOK || field(body, "document") != nil || field(body, "document_error") != want ||
			field(body, "name") != "PLA" {
			t.Errorf("%s: %d %s", want, status, jsonv.Dumps(body))
		}
	}
}

func TestAnUnknownPresetIsNotFound(t *testing.T) {
	e := newEnv(t)
	for _, id := range []string{"9", "0", "-1", "bambuddy-import", "99999999999999999999", "+1", "01"} {
		status, _, body := e.get(listPath + "/" + id)
		if status != http.StatusNotFound || field(body, "code") != "preset_not_found" {
			t.Errorf("%s: %d %s", id, status, jsonv.Dumps(body))
		}
	}
}

// wire is test-fixtures/filament/preset-wire.json: one list row, one detail and one diff as
// Go writes them, which the frontend's and the agent's hand-written types are checked against.
func wire(t *testing.T) jsonv.Object {
	t.Helper()
	_, file, _, _ := runtime.Caller(0)
	raw, err := os.ReadFile(filepath.Join(filepath.Dir(file), "../../../../../test-fixtures/filament/preset-wire.json"))
	if err != nil {
		t.Fatal(err)
	}
	v, err := jsonv.Parse(raw)
	if err != nil {
		t.Fatal(err)
	}
	return v.(jsonv.Object)
}

func sameKeys(t *testing.T, what string, got, want any) {
	t.Helper()
	g, w := keys(got), keys(want)
	slices.Sort(g)
	slices.Sort(w)
	if !slices.Equal(g, w) {
		t.Errorf("%s keys\n got %v\nwant %v", what, g, w)
	}
}

func TestTheAnswersHaveTheWireKeys(t *testing.T) {
	e := newEnv(t)
	id := e.preset("manual", "PLA", `{"name":"PLA","a":"1"}`, `{"name":"PLA","a":"2"}`)
	fixture := wire(t)
	_, _, list := e.get(listPath)
	sameKeys(t, "row", list.([]any)[0], field(fixture, "row"))
	_, _, detail := e.get(fmt.Sprintf("%s/%d", listPath, id))
	sameKeys(t, "detail", detail, field(fixture, "detail"))
	sameKeys(t, "version", field(detail, "versions").([]any)[0], field(field(fixture, "detail"), "versions").([]any)[0])
	_, _, diff := e.get(fmt.Sprintf("%s/%d/diff", listPath, id))
	sameKeys(t, "diff", diff, field(fixture, "diff"))
	sameKeys(t, "change", field(diff, "changes").([]any)[0], field(field(fixture, "diff"), "changes").([]any)[0])
}
