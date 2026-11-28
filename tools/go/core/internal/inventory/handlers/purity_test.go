package handlers_test

import (
	"go/ast"
	"go/parser"
	"go/token"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"testing"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/inventory/modeldata"
)

// TestTheBaseFlowNamesNoKind is design §2's anti-goal: no `if kind == …` in the base flow
// or a default handler. No string literal in handlers/*.go or flow/*.go (their tests and
// catalog.go, which is the catalog's data, aside) is a kind id of kinds.yaml or a special
// type id.
func TestTheBaseFlowNamesNoKind(t *testing.T) {
	dir, err := modeldata.Dir()
	if err != nil {
		t.Fatal(err)
	}
	m, err := modeldata.Load(dir)
	if err != nil {
		t.Fatal(err)
	}
	named := map[string]string{}
	for _, k := range m.Kinds {
		named[k.ID] = "kind"
	}
	for id := range m.Special {
		named[id] = "special type"
	}
	if len(named) < 30 {
		t.Fatalf("only %d ids read from the proof", len(named))
	}

	_, here, _, _ := runtime.Caller(0)
	pkg := filepath.Dir(here)
	var files []string
	for _, pattern := range []string{filepath.Join(pkg, "*.go"), filepath.Join(pkg, "..", "flow", "*.go")} {
		found, err := filepath.Glob(pattern)
		if err != nil {
			t.Fatal(err)
		}
		files = append(files, found...)
	}
	checked := 0
	for _, path := range files {
		if strings.HasSuffix(path, "_test.go") || filepath.Base(path) == "catalog.go" {
			continue
		}
		checked++
		fset := token.NewFileSet()
		file, err := parser.ParseFile(fset, path, nil, 0)
		if err != nil {
			t.Fatal(err)
		}
		ast.Inspect(file, func(n ast.Node) bool {
			lit, ok := n.(*ast.BasicLit)
			if !ok || lit.Kind != token.STRING {
				return true
			}
			value, err := strconv.Unquote(lit.Value)
			if err != nil {
				t.Fatalf("%s: %v", fset.Position(lit.Pos()), err)
			}
			if what, ok := named[value]; ok {
				t.Errorf("%s: the literal %q is a %s id", fset.Position(lit.Pos()), value, what)
			}
			return true
		})
	}
	if checked < 5 {
		t.Fatalf("only %d files checked", checked)
	}
}
