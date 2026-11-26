package routes

import (
	"errors"
	"slices"
	"strings"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/httpx"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/inventory/problem"
)

// Codes are the problem codes of the inventory's routes and activities, with their status,
// as inventory/problems.py PROBLEMS writes them: type problem.Base plus the code, and the
// code as a member. bad_property_schema and the entity reference codes keep the entities'
// type. K1's new codes (R-K1-23): step_not_applicable, step_incomplete, handler_not_built,
// wrong_counting and recost_orders_moved (409); steps_invalid, class_changed, class_open,
// class_invalid and bad_input (422). A steps_invalid answer lists problems: [{owner?, step,
// handler, reason, missing?}]; a class_changed answer names the key it changes (owner, key,
// from, to).
var Codes = map[string]int{
	// 404
	"kind_not_found": 404, "item_not_found": 404, "lot_not_found": 404, "unit_not_found": 404,
	"location_not_found": 404, "entity_not_found": 404, "operation_not_found": 404,
	// 409
	"kind_exists": 409, "kind_in_use": 409, "kind_archived": 409, "item_exists": 409,
	"lot_empty": 409, "unit_installed": 409, "serial_exists": 409, "filament_item_refused": 409,
	"step_not_applicable": 409, "step_incomplete": 409, "handler_not_built": 409,
	"wrong_counting": 409, "recost_orders_moved": 409,
	// 422
	"kind_cycle": 422, "bad_quantity": 422, "bad_property_schema": 422, "bad_entity_ref": 422,
	"unknown_entity_kind": 422, "steps_invalid": 422, "class_changed": 422, "class_open": 422,
	"class_invalid": 422, "bad_input": 422, "bad_limit": 422,
	// 500: an activity that Temporal closed without an answer (an operation's error only).
	"inventory_activity_failed": 500,
}

// problemOf is a read's error as the route answers it: a refusal as its problem document,
// every other error as it is (crud answers a database out of reach with a 503).
func problemOf(err error) error {
	var refused *problem.Refused
	if !errors.As(err, &refused) {
		return err
	}
	body := refused.Body()
	p := &httpx.Problem{Status: refused.Status, Detail: refused.Detail, Type: body["type"].(string)}
	keys := make([]string, 0, len(body))
	for k := range body {
		switch k {
		case "type", "title", "status", "detail":
		default:
			keys = append(keys, k)
		}
	}
	// code first, as core/problems.py ApiError writes its extensions; the rest by name.
	slices.SortFunc(keys, func(a, b string) int {
		if a == "code" || b == "code" {
			return map[bool]int{true: -1, false: 1}[a == "code"]
		}
		return strings.Compare(a, b)
	})
	for _, k := range keys {
		p.Extensions = append(p.Extensions, httpx.Field{Key: k, Value: body[k]})
	}
	return p
}

// refuse is the problem code at its status, with detail.
func refuse(code, detail string) error {
	return problemOf(&problem.Refused{Status: Codes[code], Code: code, Detail: detail})
}
