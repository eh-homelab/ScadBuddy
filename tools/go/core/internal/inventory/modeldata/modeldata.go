// Package modeldata loads the inventory model's YAML proof (docs/inventory-model/) for
// the tests. Only this package imports the YAML library, and only test files import this
// package (R-K1-20; TestOnlyTestsImportModeldata checks it).
package modeldata

import (
	"fmt"
	"os"
	"path/filepath"

	"go.yaml.in/yaml/v3"
)

type Param struct {
	Type     string   `yaml:"type"`
	Required bool     `yaml:"required"`
	Enum     []string `yaml:"enum"`
}

type Handler struct {
	ID      string            `yaml:"id"`
	Steps   []string          `yaml:"steps"`
	Applies map[string]string `yaml:"applies"`
	Params  map[string]Param  `yaml:"params"`
}

type Column struct {
	Match    map[string]string `yaml:"match"`
	Handlers map[string]string `yaml:"handlers"`
}

type Special struct {
	Needs              map[string]string `yaml:"needs"`
	Handlers           map[string]string `yaml:"handlers"`
	RequiresProperties []string          `yaml:"requires_properties"`
	Panels             []string          `yaml:"panels"`
	Tools              []string          `yaml:"tools"`
}

type StepConf struct {
	Handler string         `yaml:"handler"`
	Params  map[string]any `yaml:"params"`
}

type Kind struct {
	ID      string              `yaml:"id"`
	Name    string              `yaml:"name"`
	Parent  string              `yaml:"parent"`
	Special string              `yaml:"special"`
	UOM     string              `yaml:"uom"`
	Class   map[string]string   `yaml:"class"`
	Steps   map[string]StepConf `yaml:"steps"`
	Schema  map[string]any      `yaml:"schema"`
	Seed    bool                `yaml:"seed"`
	Was     map[string]any      `yaml:"was"`
}

type Item struct {
	ID         string              `yaml:"id"`
	Name       string              `yaml:"name"`
	Kind       string              `yaml:"kind"`
	Properties map[string]any      `yaml:"properties"`
	Steps      map[string]StepConf `yaml:"steps"`
}

type Instance struct {
	ID       string `yaml:"id"`
	Item     string `yaml:"item"`
	Type     string `yaml:"type"`
	StartsAt string `yaml:"starts_at"`
	EndsAt   string `yaml:"ends_at"`
}

type PropertyRow struct {
	Key   string `yaml:"key"`
	At    string `yaml:"at"`
	Op    string `yaml:"op"`
	Old   any    `yaml:"old"`
	New   any    `yaml:"new"`
	Actor string `yaml:"actor"`
}

// Model is the whole proof: the handler catalog, the kind tree and the examples.
type Model struct {
	Steps                 []string
	Handlers              []Handler
	Defaults              map[string]Column
	Special               map[string]Special
	Kinds                 []Kind
	Items                 []Item
	Instances             []Instance
	Relations             []map[string]any
	History               []map[string]any
	ExpectPropertyHistory map[string][]PropertyRow
	Readme                string
}

// proofDir is the proof's folder, relative to the repo root.
const proofDir = "docs/inventory-model"

// Dir is the repo's docs/inventory-model. It walks up from the working directory (a
// test's package directory) to the first directory that holds that folder.
func Dir() (string, error) {
	wd, err := os.Getwd()
	if err != nil {
		return "", err
	}
	return dirFrom(wd)
}

func dirFrom(start string) (string, error) {
	for dir := start; ; {
		path := filepath.Join(dir, proofDir)
		if info, err := os.Stat(path); err == nil && info.IsDir() {
			return path, nil
		}
		parent := filepath.Dir(dir)
		if parent == dir {
			return "", fmt.Errorf("no %s folder in %s or a directory above it", proofDir, start)
		}
		dir = parent
	}
}

func read(dir, name string, out any) error {
	b, err := os.ReadFile(filepath.Join(dir, name))
	if err != nil {
		return err
	}
	if err := yaml.Unmarshal(b, out); err != nil {
		return fmt.Errorf("%s: %w", name, err)
	}
	return nil
}

// Load reads the proof's files from dir.
func Load(dir string) (*Model, error) {
	var h struct {
		Steps    yaml.Node          `yaml:"steps"`
		Handlers []Handler          `yaml:"handlers"`
		Defaults map[string]Column  `yaml:"defaults"`
		Special  map[string]Special `yaml:"special_types"`
	}
	if err := read(dir, "handlers.yaml", &h); err != nil {
		return nil, err
	}
	var k struct {
		Kinds []Kind `yaml:"kinds"`
	}
	if err := read(dir, "kinds.yaml", &k); err != nil {
		return nil, err
	}
	var x struct {
		Items                 []Item                   `yaml:"items"`
		Instances             []Instance               `yaml:"instances"`
		Relations             []map[string]any         `yaml:"relations"`
		History               []map[string]any         `yaml:"history"`
		ExpectPropertyHistory map[string][]PropertyRow `yaml:"expect_property_history"`
	}
	if err := read(dir, "examples.yaml", &x); err != nil {
		return nil, err
	}
	readme, err := os.ReadFile(filepath.Join(dir, "README.md"))
	if err != nil {
		return nil, err
	}
	m := &Model{
		Handlers:              h.Handlers,
		Defaults:              h.Defaults,
		Special:               h.Special,
		Kinds:                 k.Kinds,
		Items:                 x.Items,
		Instances:             x.Instances,
		Relations:             x.Relations,
		History:               x.History,
		ExpectPropertyHistory: x.ExpectPropertyHistory,
		Readme:                string(readme),
	}
	// The steps are a map in the file; its order is the base flow's order.
	for i := 0; i+1 < len(h.Steps.Content); i += 2 {
		m.Steps = append(m.Steps, h.Steps.Content[i].Value)
	}
	return m, nil
}
