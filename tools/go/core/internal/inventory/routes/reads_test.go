package routes

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"testing"

	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/auth"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/crud"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/inventory/store"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/testdb"
)

func TestMain(m *testing.M) { testdb.Main(m) }

type anyone struct{}

func (anyone) Resolve(context.Context, string) *auth.Service {
	return &auth.Service{Caller: "inventory", Scopes: []string{"crud.inventory"}}
}

type closed struct{}

func (closed) Closed(context.Context, string, string) (store.Closure, error) {
	return store.Closure{State: store.Failed, Detail: "activity timed out"}, nil
}

func get(t *testing.T, pool *pgxpool.Pool, path string) (int, map[string]any) {
	t.Helper()
	router := &crud.Router{Resolver: anyone{}, Next: http.NotFoundHandler()}
	router.Add("inventory", Reads(pool, closed{})...)
	w := httptest.NewRecorder()
	router.ServeHTTP(w, httptest.NewRequest("GET", crud.Prefix+"inventory"+path, nil))
	var out map[string]any
	_ = json.Unmarshal(w.Body.Bytes(), &out)
	if w.Header().Get("X-ScadBuddy-Crud") != "1" {
		t.Fatalf("GET %s: no CRUD header", path)
	}
	return w.Code, out
}

// loc is the first issue's loc of a 422, joined by dots.
func loc(t *testing.T, body map[string]any) string {
	t.Helper()
	issues, _ := body["errors"].([]any)
	if len(issues) == 0 {
		t.Fatalf("no errors: %v", body)
	}
	var parts []string
	for _, p := range issues[0].(map[string]any)["loc"].([]any) {
		parts = append(parts, p.(string))
	}
	return strings.Join(parts, ".")
}

func TestAQueryOrAPathOutOfShapeIsA422WithItsLoc(t *testing.T) {
	pool := testdb.New(t)
	nul := "%00"
	for path, want := range map[string]string{
		"/items?limit=0":                       "query.limit",
		"/items?limit=5001":                    "query.limit",
		"/items?kind=Bad-Kind":                 "query.kind",
		"/items?q=a" + nul:                     "query.q",
		"/items?compatible=" + nul:             "query.compatible",
		"/items?q=" + strings.Repeat("x", 201): "query.q",
		"/items?low=maybe":                     "query.low",
		"/kinds?archived=maybe":                "query.archived",
		"/kinds/Bad":                           "path.kind_id",
		"/kinds/Bad/schema":                    "path.kind_id",
		"/items/nope":                          "path.item_id",
		"/items/nope/lots":                     "path.item_id",
		"/items/nope/units":                    "path.item_id",
		"/lots/nope":                           "path.lot_id",
		"/units/nope":                          "path.unit_id",
		"/moves?item_id=nope":                  "query.item_id",
		"/moves?limit=1001":                    "query.limit",
		"/moves?order_item_id=" + nul:          "query.order_item_id",
		"/operations/one":                      "query.id",
		"/items/0b7f6a4e-5d3c-4b2a-9e1f-123456789abc/estimate":          "query.qty",
		"/items/0b7f6a4e-5d3c-4b2a-9e1f-123456789abc/units?status=lost": "query.status",
	} {
		status, body := get(t, pool, path)
		if status != 422 {
			t.Errorf("%s: %d %v", path, status, body)
			continue
		}
		if got := loc(t, body); got != want {
			t.Errorf("%s: loc %s, want %s", path, got, want)
		}
	}
}

func TestAMissingEntityIsItsProblem(t *testing.T) {
	pool := testdb.New(t)
	missing := "0B7F6A4E5D3C4B2A9E1F123456789ABC" // any case, no hyphens: Python's UUID reads it
	for path, code := range map[string]string{
		"/items/" + missing:                     "item_not_found",
		"/items/" + missing + "/lots":           "item_not_found",
		"/items/" + missing + "/units":          "item_not_found",
		"/items/" + missing + "/estimate?qty=1": "item_not_found",
		"/lots/" + missing:                      "lot_not_found",
		"/units/" + missing:                     "unit_not_found",
		"/kinds/nope_kind":                      "kind_not_found",
		"/kinds/nope_kind/schema":               "kind_not_found",
		"/operations/one?id=browser::k":         "operation_not_found",
	} {
		status, body := get(t, pool, path)
		if status != 404 || body["code"] != code || body["type"] != "https://scadbuddy.dev/problems/inventory/"+code {
			t.Errorf("%s: %d %v", path, status, body)
		}
	}
	_, body := get(t, pool, "/lots/"+missing)
	if body["detail"] != "no lot '0b7f6a4e-5d3c-4b2a-9e1f-123456789abc'" {
		t.Errorf("%v", body["detail"])
	}
	if status, body := get(t, pool, "/moves?item_id="+missing); status != 200 || len(body["moves"].([]any)) != 0 {
		t.Errorf("%d %v", status, body)
	}
}

func TestAnOperationIsReadByItsQueryID(t *testing.T) {
	pool := testdb.New(t)
	ctx := context.Background()
	op := store.Operation{ID: "browser:p1:k", Kind: "inventory.Take", Subject: "item:x", Attempt: 1}
	if err := store.StartOperation(ctx, pool, op); err != nil {
		t.Fatal(err)
	}
	status, body := get(t, pool, "/operations/one?id=browser:p1:k")
	if status != 200 || body["id"] != "browser:p1:k" || body["status"] != "running" || body["kind"] != "inventory.Take" {
		t.Fatalf("%d %v", status, body)
	}
	// Past a minute, the route asks Temporal, which says the activity closed.
	if _, err := pool.Exec(ctx, `UPDATE inventory_operations SET updated_at = now() - interval '2 minutes'`); err != nil {
		t.Fatal(err)
	}
	_, body = get(t, pool, "/operations/one?id=browser:p1:k")
	e, _ := body["error"].(map[string]any)
	if body["status"] != "failed" || e == nil || e["extensions"].(map[string]any)["code"] != "inventory_activity_failed" {
		t.Fatalf("%v", body)
	}
}

func TestAnEstimateRefusesABadQuantity(t *testing.T) {
	pool := testdb.New(t)
	for _, qty := range []string{"x", "1e3", "-1", "0"} {
		status, body := get(t, pool, "/items/0b7f6a4e-5d3c-4b2a-9e1f-123456789abc/estimate?qty="+qty)
		if status == 200 {
			t.Errorf("%s: %v", qty, body)
		}
	}
	if status, body := get(t, pool, "/items/0b7f6a4e-5d3c-4b2a-9e1f-123456789abc/estimate?qty=x"); status != 422 || body["code"] != "bad_quantity" {
		t.Errorf("%d %v", status, body)
	}
}

// codeLiteral finds a refusal's code and status in the inventory's sources.
var codeLiteral = regexp.MustCompile(`Status: *(\d+), *Code: *"([a-z_]+)"|Refused\((\d+), *"([a-z_]+)"`)

func TestEveryCodeTheInventoryAnswersIsListedWithItsStatus(t *testing.T) {
	files, err := filepath.Glob("../*/*.go")
	if err != nil {
		t.Fatal(err)
	}
	seen := 0
	for _, f := range files {
		if strings.HasSuffix(f, "_test.go") {
			continue
		}
		src, err := os.ReadFile(f)
		if err != nil {
			t.Fatal(err)
		}
		for _, m := range codeLiteral.FindAllStringSubmatch(string(src), -1) {
			status, code := m[1], m[2]
			if code == "" {
				status, code = m[3], m[4]
			}
			want, ok := Codes[code]
			if !ok || strconv.Itoa(want) != status {
				t.Errorf("%s: %s %s is not in Codes with that status (%d)", f, status, code, want)
			}
			seen++
		}
	}
	if seen < 20 {
		t.Fatalf("found only %d codes: the pattern no longer matches the sources", seen)
	}
}

func TestARefusalIsTheProblemDocumentWithItsMembers(t *testing.T) {
	err := problemOf(&store.Refused{Status: 422, Code: "steps_invalid", Detail: "bad",
		Problems: []store.StepProblem{{Step: "consume", Handler: "consume.lot_fifo", Reason: "unknown_handler"}},
		Fields:   map[string]any{"owner": "item:x"}})
	w := httptest.NewRecorder()
	router := &crud.Router{Resolver: anyone{}, Next: http.NotFoundHandler()}
	router.Add("inventory", crud.Route{Method: "GET", Path: "/x", Handle: func(*http.Request, crud.Params) (any, error) { return nil, err }})
	router.ServeHTTP(w, httptest.NewRequest("GET", crud.Prefix+"inventory/x", nil))
	if w.Code != 422 || w.Header().Get("Content-Type") != "application/problem+json" {
		t.Fatalf("%d %s", w.Code, w.Header())
	}
	var body map[string]any
	if err := json.Unmarshal(w.Body.Bytes(), &body); err != nil {
		t.Fatal(err)
	}
	problems, _ := body["problems"].([]any)
	if body["code"] != "steps_invalid" || body["owner"] != "item:x" || len(problems) != 1 ||
		body["type"] != "https://scadbuddy.dev/problems/inventory/steps_invalid" {
		t.Fatalf("%s", w.Body)
	}
	if !strings.HasPrefix(w.Body.String(), `{"type":`) {
		t.Fatalf("%s", w.Body)
	}
}
