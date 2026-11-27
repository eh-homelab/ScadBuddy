package filamentpresets

import (
	"errors"
	"math"
	"net/http"
	"slices"
	"strconv"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/httpx"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/jsonv"
)

// KeyChange is one key whose value differs between two documents; nil (null) is absent.
type KeyChange struct {
	Key    string `json:"key"`
	Before any    `json:"before"`
	After  any    `json:"after"`
}

// ChangedKeys is profile_fetch.py `_changed_keys`, sorted(k for k in {*old, *new} if
// old.get(k) != new.get(k)), with each value before and after. Two values are compared by
// their JSON text (jsonv.Dumps), not by Python's != (plan ruling N9): Bambu Studio's values
// are strings and string lists, where the two agree. An absent key reads as null, as
// Python's get reads it.
func ChangedKeys(old, new jsonv.Object) []KeyChange {
	seen := map[string]bool{}
	var names []string
	for _, doc := range []jsonv.Object{old, new} {
		for _, m := range doc {
			if !seen[m.Key] {
				seen[m.Key] = true
				names = append(names, m.Key)
			}
		}
	}
	// Go's byte order of UTF-8 is Python's code point order.
	slices.Sort(names)
	out := []KeyChange{}
	for _, k := range names {
		before, _ := old.Get(k)
		after, _ := new.Get(k)
		if jsonv.Dumps(before) != jsonv.Dumps(after) {
			out = append(out, KeyChange{Key: k, Before: before, After: after})
		}
	}
	return out
}

// Diff is the keys that changed from one version of a preset to another.
type Diff struct {
	PresetID    int64       `json:"preset_id"`
	FromVersion int64       `json:"from_version"`
	ToVersion   int64       `json:"to_version"`
	Changes     []KeyChange `json:"changes"`
}

// versionParam is the query's name, its value when given, and false when it is given and
// no positive integer.
func versionParam(r *http.Request, name string) (int64, bool, bool) {
	query := r.URL.Query()
	if !query.Has(name) {
		return 0, false, true
	}
	s := query.Get(name)
	if !positive.MatchString(s) {
		return 0, true, false
	}
	n, err := strconv.ParseInt(s, 10, 64)
	if err != nil {
		// A positive integer past int64 names no version.
		n = math.MaxInt64
	}
	return n, true, true
}

func serveDiff(w http.ResponseWriter, r *http.Request, pool *pgxpool.Pool, blobs Blobs) {
	id, ok := presetID(r)
	if !ok {
		writeCode(w, r, presetNotFound)
		return
	}
	from, fromGiven, fromOK := versionParam(r, "from_version")
	to, toGiven, toOK := versionParam(r, "to_version")
	var issues []httpx.Issue
	for _, p := range []struct {
		name string
		ok   bool
	}{{"from_version", fromOK}, {"to_version", toOK}} {
		if !p.ok {
			issues = append(issues, httpx.Issue{Loc: []string{"query", p.name}, Msg: p.name + " is a positive integer"})
		}
	}
	if len(issues) > 0 {
		httpx.WriteProblem(w, r, httpx.Invalid(issues...))
		return
	}
	ctx := r.Context()
	var current int64
	err := pool.QueryRow(ctx, `SELECT version FROM filament_presets WHERE id = $1`, id).Scan(&current)
	if errors.Is(err, pgx.ErrNoRows) {
		writeCode(w, r, presetNotFound)
		return
	}
	if err != nil {
		failed(w, r, err)
		return
	}
	if !toGiven {
		to = current
	}
	if !fromGiven {
		from = max(to-1, 1)
	}
	keys := map[int64]string{}
	res, err := pool.Query(ctx, `SELECT version, blob_key FROM filament_preset_versions
		WHERE preset_id = $1 AND version = ANY($2::bigint[])`, id, []int64{from, to})
	if err != nil {
		failed(w, r, err)
		return
	}
	for res.Next() {
		var v int64
		var key string
		if err := res.Scan(&v, &key); err != nil {
			res.Close()
			failed(w, r, err)
			return
		}
		keys[v] = key
	}
	res.Close()
	if err := res.Err(); err != nil {
		failed(w, r, err)
		return
	}
	if keys[from] == "" || keys[to] == "" {
		writeCode(w, r, versionNotFound)
		return
	}
	docs := map[int64]jsonv.Object{}
	for _, v := range []int64{from, to} {
		if _, done := docs[v]; done {
			continue
		}
		doc, code, err := readDocument(ctx, blobs, keys[v])
		if code != "" {
			logRead(r, code, err)
			writeCode(w, r, code)
			return
		}
		docs[v] = StripCredentials(doc)
	}
	answer(w, r, Diff{PresetID: id, FromVersion: from, ToVersion: to, Changes: ChangedKeys(docs[from], docs[to])})
}
