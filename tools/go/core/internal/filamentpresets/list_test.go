package filamentpresets

import (
	"context"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/jsonv"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/testdb"
)

func TestMain(m *testing.M) { testdb.Main(m) }

// env is the area's routes over a test database and a store in memory.
type env struct {
	t     *testing.T
	pool  *pgxpool.Pool
	blobs *fakeBlobs
	mux   *http.ServeMux
}

func newEnv(t *testing.T) *env {
	t.Helper()
	e := &env{t: t, pool: testdb.New(t), blobs: newFake(), mux: http.NewServeMux()}
	for _, p := range Public(e.pool, e.blobs) {
		e.mux.Handle(p.Pattern, p.Handler)
	}
	return e
}

func (e *env) exec(sql string, args ...any) {
	e.t.Helper()
	if _, err := e.pool.Exec(context.Background(), sql, args...); err != nil {
		e.t.Fatal(err)
	}
}

func (e *env) text(sql string, args ...any) string {
	e.t.Helper()
	var s string
	if err := e.pool.QueryRow(context.Background(), sql, args...).Scan(&s); err != nil {
		e.t.Fatal(err)
	}
	return s
}

// get answers path's status, its Cache-Control and its body read with order kept.
func (e *env) get(path string) (int, string, any) {
	e.t.Helper()
	rec := httptest.NewRecorder()
	e.mux.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, path, nil))
	raw, _ := io.ReadAll(rec.Body)
	v, err := jsonv.Parse(raw)
	if err != nil {
		e.t.Fatalf("%s: %d %q", path, rec.Code, raw)
	}
	return rec.Code, rec.Header().Get("Cache-Control"), v
}

// preset inserts a preset as sb_presets.create_on and version_on write it: the row at the
// last document's version, one version row and one blob_refs hold per document, and each
// document kept in the store as its packed blob.
func (e *env) preset(source, name string, docs ...string) int64 {
	e.t.Helper()
	key, sha := keyOf(docs[len(docs)-1])
	var id int64
	err := e.pool.QueryRow(context.Background(), `INSERT INTO filament_presets
		(name, source, version, blob_key, sha256, filament_type, compatible_printers)
		VALUES ($1, $2, $3, $4, $5, 'PLA', '{"Bambu Lab H2C 0.4 nozzle"}') RETURNING id`,
		name, source, len(docs), key, sha).Scan(&id)
	if err != nil {
		e.t.Fatal(err)
	}
	for i, doc := range docs {
		k, s := keyOf(doc)
		e.blobs.put(k, blob{body: packed(e.t, map[string]string{"file": doc})})
		e.exec(`INSERT INTO filament_preset_versions (preset_id, version, blob_key, sha256, made_by)
			VALUES ($1, $2, $3, $4, 'test')`, id, i+1, k, s)
		e.exec(`INSERT INTO blob_refs (key, holder_kind, holder_id) VALUES ($1, 'filament_preset', $2)
			ON CONFLICT DO NOTHING`, k, fmt.Sprintf("%d:%d", id, i+1))
	}
	return id
}

func field(v any, key string) any {
	got, _ := v.(jsonv.Object).Get(key)
	return got
}

func number(v any, key string) string {
	n, _ := field(v, key).(interface{ String() string })
	if n == nil {
		return fmt.Sprint(field(v, key))
	}
	return n.String()
}

func TestListCountsBindingsAndFilters(t *testing.T) {
	e := newEnv(t)
	fetched := e.preset("3dfp", "b fetched", `{"name":"b fetched"}`)
	e.preset("bambuddy", "A bambuddy", `{"name":"A bambuddy"}`)
	manual := e.preset("manual", "c manual", `{"name":"c manual"}`)
	material := e.text(`SELECT id::text FROM filament_materials WHERE name = 'PLA'`)
	typeID := e.text(`INSERT INTO filament_spool_types (code, material_id, rgba, label_weight_g)
		VALUES ('T-1', $1, '#FF0000FF', 1000) RETURNING id::text`, material)
	e.exec(`UPDATE filament_presets SET type_id = $1 WHERE id = $2`, typeID, fetched)
	ref := fmt.Sprintf("sb-%d", fetched)
	e.exec(`INSERT INTO filament_spool_type_presets (type_id, printer_model, nozzle_diameter, slicer_filament)
		VALUES ($1, 'H2C', '0.4', $2)`, typeID, ref)
	for i := range 2 {
		spool := e.text(`INSERT INTO filament_spools (material_id, rgba, label_weight_g)
			VALUES ($1, '#FF0000FF', 1000) RETURNING id::text`, material)
		e.exec(`INSERT INTO filament_spool_presets (spool_id, printer_model, nozzle_diameter, slicer_filament)
			VALUES ($1, 'H2C', $2, $3)`, spool, fmt.Sprintf("0.%d", i+4), ref)
	}

	status, cache, body := e.get(listPath)
	if status != http.StatusOK || cache != "no-store" {
		t.Fatalf("%d %q", status, cache)
	}
	rows := body.([]any)
	if len(rows) != 3 {
		t.Fatalf("%d rows", len(rows))
	}
	// lower(name), then id.
	if names := []any{field(rows[0], "name"), field(rows[1], "name"), field(rows[2], "name")}; fmt.Sprint(names) != "[A bambuddy b fetched c manual]" {
		t.Errorf("order %v", names)
	}
	row := rows[1]
	if number(row, "id") != fmt.Sprint(fetched) || field(row, "ref") != ref || field(row, "type_id") != typeID ||
		number(row, "used_by_types") != "1" || number(row, "used_by_spools") != "2" {
		t.Errorf("row %s", jsonv.Dumps(row))
	}
	if field(rows[0], "mirror") != "bambuddy_owned" || field(rows[2], "mirror") != "pending" {
		t.Errorf("mirror %v %v", field(rows[0], "mirror"), field(rows[2], "mirror"))
	}
	if (row.(jsonv.Object)).Has("blob_key") {
		t.Error("a row names its blob_key")
	}

	_, _, body = e.get(listPath + "?source=manual")
	if rows := body.([]any); len(rows) != 1 || number(rows[0], "id") != fmt.Sprint(manual) {
		t.Errorf("source=manual: %s", jsonv.Dumps(body))
	}
	_, _, body = e.get(listPath + "?type_id=" + typeID)
	if rows := body.([]any); len(rows) != 1 || number(rows[0], "id") != fmt.Sprint(fetched) {
		t.Errorf("type_id: %s", jsonv.Dumps(body))
	}
	for query, loc := range map[string]string{"source=x": "source", "source=": "source", "type_id=x": "type_id",
		"type_id=" + strings.ToUpper(typeID): "type_id"} {
		status, _, body := e.get(listPath + "?" + query)
		if status != http.StatusUnprocessableEntity || !strings.Contains(jsonv.Dumps(body), `"loc":["query","`+loc+`"]`) {
			t.Errorf("%s: %d %s", query, status, jsonv.Dumps(body))
		}
	}
}

func TestAnEmptyListIsAnEmptyArray(t *testing.T) {
	e := newEnv(t)
	if status, _, body := e.get(listPath); status != http.StatusOK || jsonv.Dumps(body) != "[]" {
		t.Fatalf("%d %s", status, jsonv.Dumps(body))
	}
}
