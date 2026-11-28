package modeldata

import (
	"go/parser"
	"go/token"
	"io/fs"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
)

// self is this package's import path.
const self = "github.com/eh-homelab/ScadBuddy/tools/go/core/internal/inventory/modeldata"

// yamlLibs are the YAML libraries' import paths: only tests and this package read YAML.
var yamlLibs = []string{"go.yaml.in/yaml/", "gopkg.in/yaml.", "sigs.k8s.io/yaml"}

func isYAML(path string) bool {
	for _, lib := range yamlLibs {
		if strings.HasPrefix(path, lib) {
			return true
		}
	}
	return false
}

func TestDirWalksUpToTheProof(t *testing.T) {
	root := t.TempDir()
	if err := os.MkdirAll(filepath.Join(root, proofDir), 0o755); err != nil {
		t.Fatal(err)
	}
	sub := filepath.Join(root, "a", "b")
	if err := os.MkdirAll(sub, 0o755); err != nil {
		t.Fatal(err)
	}
	got, err := dirFrom(sub)
	if err != nil {
		t.Fatal(err)
	}
	if want := filepath.Join(root, proofDir); got != want {
		t.Fatalf("dirFrom = %q, want %q", got, want)
	}
}

func TestDirNamesTheMissingFolder(t *testing.T) {
	_, err := dirFrom(t.TempDir())
	if err == nil || !strings.Contains(err.Error(), proofDir) {
		t.Fatalf("got %v, want an error that names %s", err, proofDir)
	}
}

// TestOnlyTestsImportModeldata is R-K1-20 ("test-only YAML"): no non-test Go file of the
// module outside this package imports it or a YAML library, so YAML stays out of the binary.
func TestOnlyTestsImportModeldata(t *testing.T) {
	wd, err := os.Getwd()
	if err != nil {
		t.Fatal(err)
	}
	root := wd
	for {
		if _, err := os.Stat(filepath.Join(root, "go.mod")); err == nil {
			break
		}
		parent := filepath.Dir(root)
		if parent == root {
			t.Fatalf("no go.mod in %s or a directory above it", wd)
		}
		root = parent
	}

	checked, testImports := 0, 0
	err = filepath.WalkDir(root, func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		name := d.Name()
		if d.IsDir() {
			if path != root && (name == "vendor" || name == "testdata" || strings.HasPrefix(name, ".") || strings.HasPrefix(name, "_")) {
				return filepath.SkipDir
			}
			return nil
		}
		if !strings.HasSuffix(name, ".go") || filepath.Dir(path) == wd {
			return nil
		}
		file, err := parser.ParseFile(token.NewFileSet(), path, nil, parser.ImportsOnly)
		if err != nil {
			return err
		}
		checked++
		for _, imp := range file.Imports {
			p, err := strconv.Unquote(imp.Path.Value)
			if err != nil {
				return err
			}
			if p != self && !isYAML(p) {
				continue
			}
			if strings.HasSuffix(name, "_test.go") {
				if p == self {
					testImports++
				}
			} else {
				t.Errorf("%s imports %s; only test files may (R-K1-20)", path, p)
			}
		}
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	if checked < 50 {
		t.Fatalf("only %d files checked under %s", checked, root)
	}
	// handlers/purity_test.go imports the package: the scan sees a real import.
	if testImports == 0 {
		t.Fatal("no test file imports modeldata; the scan finds no imports")
	}
}
