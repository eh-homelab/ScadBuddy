package modeldata

import (
	"encoding/json"
	"fmt"
	"strings"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/inventory/handlers"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/inventory/model"
)

// This file is test support: it turns the proof's YAML into the model's types. No
// production package imports it.

// ToSteps converts a layer's YAML steps.
func ToSteps(in map[string]StepConf) model.Steps {
	if in == nil {
		return nil
	}
	out := make(model.Steps, len(in))
	for k, v := range in {
		out[k] = model.StepConf{Handler: v.Handler, Params: v.Params}
	}
	return out
}

// Chain is a kind and its ancestors, root first (check.py chain).
func Chain(m *Model, kindID string) (model.Chain, error) {
	byID := make(map[string]Kind, len(m.Kinds))
	for _, k := range m.Kinds {
		byID[k.ID] = k
	}
	var out model.Chain
	for id := kindID; id != ""; id = byID[id].Parent {
		k, ok := byID[id]
		if !ok {
			return nil, fmt.Errorf("unknown kind %s", id)
		}
		if len(out) > len(m.Kinds) {
			return nil, fmt.Errorf("kind %s: the parents loop", kindID)
		}
		var schema json.RawMessage
		if k.Schema != nil {
			b, err := json.Marshal(k.Schema)
			if err != nil {
				return nil, fmt.Errorf("kind %s schema: %w", id, err)
			}
			schema = b
		}
		out = append(out, model.Kind{
			ID: k.ID, Name: k.Name, ParentID: k.Parent, Special: k.Special, UOM: k.UOM,
			Class: model.Class{
				Nature: k.Class["nature"], Role: k.Class["role"],
				Counting: k.Class["counting"], Billing: k.Class["billing"],
			},
			Steps:  ToSteps(k.Steps),
			Schema: schema,
			// seed is the YAML's "the migration seeds it".
			Builtin: k.Seed,
		})
	}
	for i, j := 0, len(out)-1; i < j; i, j = i+1, j-1 {
		out[i], out[j] = out[j], out[i]
	}
	return out, nil
}

// Matrix is the README's handler table as check.py builds it: one row per example item,
// its class, and the handler each step resolved to.
func Matrix(m *Model, cat *handlers.Catalog) (string, error) {
	rows := []string{
		"| Item | Class | " + strings.Join(m.Steps, " | ") + " |",
		"|---|---|" + strings.Repeat("---|", len(m.Steps)),
	}
	for _, it := range m.Items {
		ch, err := Chain(m, it.Kind)
		if err != nil {
			return "", fmt.Errorf("item %s: %w", it.ID, err)
		}
		res, problems, err := cat.Resolve(ch, ToSteps(it.Steps), nil)
		if err != nil {
			return "", fmt.Errorf("item %s: %w", it.ID, err)
		}
		if len(problems) > 0 {
			return "", fmt.Errorf("item %s: %+v", it.ID, problems)
		}
		label := res.Class.Nature + "/" + res.Class.Role + "/" + res.Class.Counting
		if res.Class.Nature == "service" {
			label = res.Class.Nature + "/" + res.Class.Billing
		}
		if res.Special != "" {
			label += " + " + res.Special
		}
		cells := make([]string, 0, len(m.Steps))
		for _, s := range m.Steps {
			if id := res.Steps[handlers.Step(s)].Handler; id == "refuse" {
				cells = append(cells, "—")
			} else {
				cells = append(cells, "`"+id+"`")
			}
		}
		rows = append(rows, "| "+it.Name+" | "+label+" | "+strings.Join(cells, " | ")+" |")
	}
	return strings.Join(rows, "\n"), nil
}
