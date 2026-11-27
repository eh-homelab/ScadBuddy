package filamentpresets

import (
	"os"
	"path/filepath"
	"runtime"
	"slices"
	"testing"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/jsonv"
)

// vectors is backend/tests/fixtures/filament-preset-vectors.json, which Python's own
// is_credential, strip_credentials and `_changed_keys` rule write
// (tests/filament/test_filament_preset_vectors.py), read with its members' order kept.
func vectors(t *testing.T) jsonv.Object {
	t.Helper()
	_, file, _, _ := runtime.Caller(0)
	raw, err := os.ReadFile(filepath.Join(filepath.Dir(file), "../../../../../backend/tests/fixtures/filament-preset-vectors.json"))
	if err != nil {
		t.Fatal(err)
	}
	v, err := jsonv.Parse(raw)
	if err != nil {
		t.Fatal(err)
	}
	return v.(jsonv.Object)
}

func cases(t *testing.T, name string) []jsonv.Object {
	t.Helper()
	list, _ := vectors(t).Get(name)
	var out []jsonv.Object
	for _, c := range list.([]any) {
		out = append(out, c.(jsonv.Object))
	}
	if len(out) == 0 {
		t.Fatalf("no %s vectors", name)
	}
	return out
}

func member(c jsonv.Object, key string) any {
	v, _ := c.Get(key)
	return v
}

func TestIsCredentialMatchesPython(t *testing.T) {
	for _, c := range cases(t, "credential_keys") {
		key, want := member(c, "key").(string), member(c, "credential").(bool)
		if got := IsCredential(key); got != want {
			t.Errorf("IsCredential(%q) = %v, Python says %v", key, got, want)
		}
	}
}

func TestStripCredentialsMatchesPython(t *testing.T) {
	for _, c := range cases(t, "documents") {
		in, out := member(c, "in").(jsonv.Object), member(c, "out").(jsonv.Object)
		if got, want := jsonv.Dumps(StripCredentials(in)), jsonv.Dumps(out); got != want {
			t.Errorf("StripCredentials(%s)\n got %s\nwant %s", jsonv.Dumps(in), got, want)
		}
	}
}

func TestChangedKeysMatchPython(t *testing.T) {
	for _, c := range cases(t, "diffs") {
		old, nu := member(c, "old").(jsonv.Object), member(c, "new").(jsonv.Object)
		want := []string{}
		for _, k := range member(c, "changed").([]any) {
			want = append(want, k.(string))
		}
		got := []string{}
		for _, ch := range ChangedKeys(StripCredentials(old), StripCredentials(nu)) {
			got = append(got, ch.Key)
		}
		if !slices.Equal(got, want) {
			t.Errorf("ChangedKeys(%s, %s) = %v, Python says %v", jsonv.Dumps(old), jsonv.Dumps(nu), got, want)
		}
	}
}

func TestChangedKeysGiveBeforeAndAfter(t *testing.T) {
	old := jsonv.MustParse(`{"a":"1","b":["2"],"c":"3"}`).(jsonv.Object)
	nu := jsonv.MustParse(`{"a":"1","b":["4"],"d":"5"}`).(jsonv.Object)
	got := ChangedKeys(old, nu)
	want := `[{"key":"b","before":["2"],"after":["4"]},{"key":"c","before":"3","after":null},{"key":"d","before":null,"after":"5"}]`
	var gotText []any
	for _, ch := range got {
		gotText = append(gotText, jsonv.Object{{Key: "key", Value: ch.Key}, {Key: "before", Value: ch.Before}, {Key: "after", Value: ch.After}})
	}
	if s := jsonv.Dumps(gotText); s != want {
		t.Errorf("\n got %s\nwant %s", s, want)
	}
}

func TestMirrorStateCoversEachCase(t *testing.T) {
	s, other := "aa", "bb"
	failure, note := "Bambuddy: 500", "Bambuddy also holds a preset of this name"
	for _, c := range []struct {
		source   string
		mirrored *string
		err      *string
		want     string
	}{
		{"bambuddy", &s, nil, "bambuddy_owned"},
		{"manual", nil, nil, "pending"},
		{"manual", &other, &failure, "failed"},
		{"3dfp", &s, nil, "sent"},
		{"3dfp", &s, &note, "sent"},
	} {
		if got := MirrorState(c.source, s, c.mirrored, c.err); got != c.want {
			t.Errorf("MirrorState(%s, %v, %v) = %s, want %s", c.source, c.mirrored, c.err, got, c.want)
		}
	}
}
