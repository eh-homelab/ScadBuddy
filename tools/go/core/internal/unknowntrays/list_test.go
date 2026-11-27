package unknowntrays

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/testdb"
)

func TestMain(m *testing.M) { testdb.Main(m) }

func exec(t *testing.T, pool *pgxpool.Pool, sql string, args ...any) {
	t.Helper()
	if _, err := pool.Exec(context.Background(), sql, args...); err != nil {
		t.Fatal(err)
	}
}

func text(t *testing.T, pool *pgxpool.Pool, sql string, args ...any) string {
	t.Helper()
	var s string
	if err := pool.QueryRow(context.Background(), sql, args...).Scan(&s); err != nil {
		t.Fatal(err)
	}
	return s
}

const printerID = "5d0c8f1e-0000-4000-8000-0000000071aa"

func printer(t *testing.T, pool *pgxpool.Pool) {
	t.Helper()
	exec(t, pool, `INSERT INTO printers (id, serial, name, model) VALUES ($1, 'SN1', 'H2C', 'H2C')`, printerID)
}

func unknown(t *testing.T, pool *pgxpool.Pool, ams, tray int, material, rgba string) string {
	t.Helper()
	return text(t, pool, `INSERT INTO filament_unknown_trays
		(printer_id, ams_id, tray_id, material, colour_rgba, first_seen_at, seen_at)
		VALUES ($1, $2, $3, $4, $5, now(), now()) RETURNING id::text`, printerID, ams, tray, material, rgba)
}

func material(t *testing.T, pool *pgxpool.Pool, name string) string {
	t.Helper()
	return text(t, pool, `SELECT id::text FROM filament_materials WHERE name = $1`, name)
}

func newSpool(t *testing.T, pool *pgxpool.Pool, materialName, rgba string) string {
	t.Helper()
	return text(t, pool, `INSERT INTO filament_spools (material_id, rgba, label_weight_g)
		VALUES ($1, $2, 1000) RETURNING id::text`, material(t, pool, materialName), rgba)
}

func TestListNamesPrinterSlotAndPlace(t *testing.T) {
	pool := testdb.New(t)
	printer(t, pool)
	id := unknown(t, pool, 0, 1, "PLA", "#FF0000FF")
	exec(t, pool, `INSERT INTO filament_locations (name, kind_id, printer_id, ams_id, tray_id)
		VALUES ('H2C AMS-A slot 2', (SELECT id FROM filament_location_kinds WHERE name = 'AMS'), $1, 0, 1)`, printerID)
	spool := newSpool(t, pool, "PLA", "#FF0000FF")
	got, err := List(context.Background(), pool)
	if err != nil {
		t.Fatal(err)
	}
	if len(got.Trays) != 1 {
		t.Fatalf("%d trays", len(got.Trays))
	}
	tray := got.Trays[0]
	if tray.ID != id || tray.Printer != "H2C" || tray.Slot != "AMS-A slot 2" {
		t.Errorf("tray %+v", tray)
	}
	if tray.PlaceName == nil || *tray.PlaceName != "H2C AMS-A slot 2" ||
		tray.PlaceCode == nil || !strings.HasPrefix(*tray.PlaceCode, "L-") {
		t.Errorf("place %v %v", tray.PlaceName, tray.PlaceCode)
	}
	if len(tray.Candidates) != 1 || tray.Candidates[0].SpoolID != spool ||
		tray.Candidates[0].Material != "PLA" || tray.Candidates[0].LabelWeightG == nil ||
		*tray.Candidates[0].LabelWeightG != 1000 || !strings.HasPrefix(tray.Candidates[0].Code, "S-") {
		t.Errorf("candidates %+v", tray.Candidates)
	}
}

func TestListLeavesOutArchivedSpoolsAndGivesEmptyArrays(t *testing.T) {
	pool := testdb.New(t)
	printer(t, pool)
	unknown(t, pool, 0, 0, "PLA", "#FF0000FF")
	spool := newSpool(t, pool, "PLA", "#FF0000FF")
	exec(t, pool, `UPDATE filament_spools SET archived_at = now() WHERE id = $1`, spool)
	got, err := List(context.Background(), pool)
	if err != nil {
		t.Fatal(err)
	}
	if got.Trays[0].Candidates == nil || len(got.Trays[0].Candidates) != 0 || got.MadeFromTags == nil {
		t.Errorf("%+v", got)
	}
}

func TestListHasTheSpoolsMadeFromTags(t *testing.T) {
	pool := testdb.New(t)
	printer(t, pool)
	recent := newSpool(t, pool, "PLA", "#FF0000FF")
	old := newSpool(t, pool, "PLA", "#00FF00FF")
	other := newSpool(t, pool, "PLA", "#0000FFFF")
	archived := newSpool(t, pool, "PLA", "#FFFF00FF")
	exec(t, pool, `UPDATE filament_spools SET archived_at = now() WHERE id = $1`, archived)
	event := func(spool, by, age string) {
		exec(t, pool, `INSERT INTO filament_spool_events (spool_id, at, kind, printer_id, ams_id, tray_id, data)
			VALUES ($1, now() - $2::interval, 'created', $3, 0, 1, jsonb_build_object('by', $4::text))`,
			spool, age, printerID, by)
	}
	event(recent, "rfid_capture", "1 hour")
	event(old, "rfid_capture", "25 hours")
	event(other, "person", "1 hour")
	event(archived, "rfid_capture", "1 hour")
	got, err := List(context.Background(), pool)
	if err != nil {
		t.Fatal(err)
	}
	if len(got.MadeFromTags) != 1 || got.MadeFromTags[0].SpoolID != recent ||
		got.MadeFromTags[0].PrinterName != "H2C" || got.MadeFromTags[0].Slot != "AMS-A slot 2" {
		t.Errorf("%+v", got.MadeFromTags)
	}
}

func dismiss(pool *pgxpool.Pool, id string) *httptest.ResponseRecorder {
	routes := Public(pool, nil)[Area]
	req := httptest.NewRequest(http.MethodPost, "/api/v1/filament/unknown-trays/"+id+"/dismiss", nil)
	req.SetPathValue("id", id)
	rec := httptest.NewRecorder()
	routes[1].Handler.ServeHTTP(rec, req)
	return rec
}

func TestDismissDeletesThenNotFound(t *testing.T) {
	pool := testdb.New(t)
	printer(t, pool)
	id := unknown(t, pool, 0, 1, "PLA", "#FF0000FF")
	keep := unknown(t, pool, 0, 2, "PLA", "#FF0000FF")
	if rec := dismiss(pool, id); rec.Code != http.StatusNoContent || rec.Body.Len() != 0 {
		t.Fatalf("first: %d %q", rec.Code, rec.Body)
	}
	rec := dismiss(pool, id)
	if rec.Code != http.StatusNotFound || !strings.Contains(rec.Body.String(), `"status":404`) {
		t.Fatalf("second: %d %q", rec.Code, rec.Body)
	}
	if rec := dismiss(pool, "not-a-uuid"); rec.Code != http.StatusNotFound {
		t.Fatalf("not a uuid: %d", rec.Code)
	}
	if n := text(t, pool, `SELECT count(*)::text FROM filament_unknown_trays WHERE id = $1`, keep); n != "1" {
		t.Errorf("another row went too: %s", n)
	}
}

func TestADatabaseOutOfReachIs503(t *testing.T) {
	pool := testdb.New(t)
	ctx, cancel := context.WithDeadline(context.Background(), time.Now().Add(-time.Second))
	defer cancel()
	routes := Public(pool, nil)[Area]
	req := httptest.NewRequest(http.MethodGet, "/api/v1/filament/unknown-trays", nil).WithContext(ctx)
	rec := httptest.NewRecorder()
	routes[0].Handler.ServeHTTP(rec, req)
	if rec.Code != http.StatusServiceUnavailable {
		t.Fatalf("%d %q", rec.Code, rec.Body)
	}
}
