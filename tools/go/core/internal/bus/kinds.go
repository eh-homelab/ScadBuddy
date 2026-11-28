package bus

// Field is one field of a typed event, after id, at and kind, as core/events.py declares it.
type Field struct {
	Name string
	// Type is "str", "int", "bool" or "list[str]".
	Type     string
	Choices  []string
	Nullable bool
	Required bool
}

func str(name string) Field     { return Field{Name: name, Type: "str", Required: true} }
func optStr(name string) Field  { return Field{Name: name, Type: "str", Nullable: true} }
func integer(name string) Field { return Field{Name: name, Type: "int", Required: true} }
func optInt(name string) Field  { return Field{Name: name, Type: "int", Nullable: true} }
func flag(name string) Field    { return Field{Name: name, Type: "bool"} }
func strs(name string) Field    { return Field{Name: name, Type: "list[str]", Required: true} }
func choice(name string, values ...string) Field {
	return Field{Name: name, Type: "str", Choices: values, Required: true}
}

// Kinds are core/events.py's typed kinds and their fields, in the order pydantic writes
// them. The shared vectors (backend/tests/fixtures/event-stream-vectors.json, "kinds") are
// what Python's models say; bus's tests check this table against them.
var Kinds = map[string][]Field{}

func kinds(fields []Field, names ...string) {
	for _, name := range names {
		Kinds[name] = fields
	}
}

func init() {
	job := []Field{str("job_id"), str("slug")}
	kinds(job, "job.pending", "job.running", "job.done", "job.failed", "job.superseded", "job.cancelled")
	kinds([]Field{str("job_id"), str("slug"), choice("stage",
		"source", "render", "split", "solids", "paint", "thumbnail", "write", "recolour")}, "job.progress")
	kinds([]Field{str("slug")}, "model.created", "model.updated", "model.deleted", "source.changed", "presets.changed")
	kinds([]Field{str("slug"), str("commit")}, "version.committed")
	kinds([]Field{str("slug"), str("upstream"), str("commit")}, "upstream.available")
	kinds([]Field{str("output_id"), str("slug")}, "output.created", "output.deleted", "output.updated",
		"print.progress", "print.settled")
	kinds([]Field{str("output_id"), str("slug"), str("run_id")}, "print.run")
	kinds([]Field{str("output_id"), str("slug"), str("run_id"),
		choice("outcome", "finished", "failed", "cancelled", "unknown"), optStr("printer")}, "print.outcome")
	kinds([]Field{str("printer_id")}, "printer.report", "printer.changed", "camera.feed", "camera.reconnect")
	kinds([]Field{str("operation_id"), str("op_kind"), str("subject")}, "operation.changed")
	kinds([]Field{str("run_id"), str("definition_id"), str("status")}, "flow_run.changed")
	kinds([]Field{str("name")}, "flow.changed", "plugin_packages.changed")
	kinds([]Field{str("task_id"), str("run_id"),
		choice("state", "open", "answered", "cancelled", "expired"), optStr("place")}, "task.changed")
	kinds([]Field{choice("slicer", "bambuddy", "native"), integer("job_id")},
		"preview_slice.progress", "preview_slice.settled")
	kinds([]Field{str("activity_id"), optStr("principal")}, "activity.changed")
	kinds([]Field{str("order_id"), str("status"), flag("cart"), flag("deleted")}, "order.changed")
	kinds([]Field{str("order_id"), str("item_id"), str("status"), optInt("attempt"), flag("cart")},
		"order_item.changed")
	kinds([]Field{str("order_id")}, "order.bank_deposits")
	kinds([]Field{str("operation_id")}, "ai_operation.changed")
	// #6949 K1: Go's own record of an inventory activity, sent as NOTIFY only.
	kinds([]Field{str("operation_id")}, "inventory_operation.changed")
	kinds([]Field{str("principal"), optStr("session_id")}, "auth.session_revoked")
	kinds([]Field{str("slug"), str("name")}, "library.changed")
	kinds([]Field{str("name"), strs("commits")}, "library.removed")
	kinds([]Field{str("family")}, "font.installed")
	kinds([]Field{choice("section", "connection", "print_options", "model_choices", "printer_bed_type",
		"printer_rack_algorithm", "model_project", "remembered")}, "settings.changed")
	kinds([]Field{str("decision_id"), str("diagnostic_id"), str("scope"), str("scope_key"),
		choice("action", "recorded", "removed")}, "analyzer.decision")
	status := Field{Name: "status", Type: "str", Nullable: true,
		Choices: []string{"running", "waiting_input", "waiting_approval", "idle", "done", "failed"}}
	kinds([]Field{str("session_id"), integer("seq"), status, optStr("replica")},
		"session.started", "session.owner", "session.waiting", "session.done", "session.message")
	kinds([]Field{optStr("last_event_id")}, "bus.resync")
}
