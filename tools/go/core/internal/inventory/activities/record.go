package activities

import (
	"context"
	"encoding/json"
	"log/slog"
	"slices"

	"github.com/jackc/pgx/v5/pgxpool"
	"go.opentelemetry.io/otel/metric"
	"go.temporal.io/sdk/activity"
	"go.temporal.io/sdk/interceptor"
	"go.temporal.io/sdk/temporal"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/inventory/store"
)

// Record is the worker interceptor that keeps each activity's record (R-K1-22): before each
// attempt it writes the row as running (store.StartOperation) and puts the operation in the
// context, so store.Write ends the row in the write's own transaction; when the attempt
// fails with an error, it notes the error (store.NoteError), and the row stays running
// while Temporal retries. An error that Temporal does not retry ends the row failed
// (store.FailOperation), since no attempt follows to end it (#8256, #8497; final). A record write that fails is logged and counted in
// scadbuddy_inventory_record_failures (meters, when not nil); it never fails the activity.
func Record(pool *pgxpool.Pool, meters metric.MeterProvider) interceptor.WorkerInterceptor {
	r := &record{pool: pool}
	if meters != nil {
		r.failures, _ = meters.Meter("scadbuddy-core/inventory").Int64Counter("scadbuddy_inventory_record_failures",
			metric.WithDescription("Writes of an inventory activity's record that failed; the activity ran on."))
	}
	return r
}

type record struct {
	interceptor.WorkerInterceptorBase
	pool     *pgxpool.Pool
	failures metric.Int64Counter
}

func (r *record) InterceptActivity(ctx context.Context, next interceptor.ActivityInboundInterceptor) interceptor.ActivityInboundInterceptor {
	in := &recordInbound{record: r}
	in.Next = next
	return in
}

type recordInbound struct {
	interceptor.ActivityInboundInterceptorBase
	*record
}

func (r *recordInbound) ExecuteActivity(ctx context.Context, in *interceptor.ExecuteActivityInput) (any, error) {
	input, ok := inputOf(in.Args)
	if !ok || input.Key == "" {
		return r.Next.ExecuteActivity(ctx, in)
	}
	info := activity.GetInfo(ctx)
	op := store.Operation{
		ID:      input.Key,
		Kind:    info.ActivityType.Name,
		Subject: subject(input),
		Attempt: int(info.Attempt),
	}
	if info.WorkflowExecution.ID != "" {
		op.WorkflowID = info.WorkflowExecution.ID
	}
	if actor, err := store.ReadActor(input.Actor); err == nil {
		op.Actor = actor
	}
	if err := store.StartOperation(ctx, r.pool, op); err != nil {
		r.failed(ctx, "could not record an inventory activity's start", op.ID, err)
		return r.Next.ExecuteActivity(ctx, in)
	}
	out, err := r.Next.ExecuteActivity(store.WithOperation(ctx, op.ID), in)
	if err != nil {
		end, msg := store.NoteError, "could not record an inventory activity's error"
		if final(err, info.Attempt, info.RetryPolicy) {
			end, msg = store.FailOperation, "could not record an inventory activity's failure"
		}
		if note := end(context.WithoutCancel(ctx), r.pool, op, err); note != nil {
			r.failed(ctx, msg, op.ID, note)
		}
	}
	return out, err
}

// final says whether Temporal gives err no next attempt: a non-retryable ApplicationError,
// an ApplicationError whose type the policy names in NonRetryableErrorTypes, or any error
// on the policy's last attempt (MaximumAttempts). A schedule-to-close timeout that ends the
// retries is not known here; a stale read asks Temporal about it (store.ReadOperation).
//
// Only err itself counts, not an ApplicationError wrapped inside it: the SDK's failure
// converter reads NonRetryable and Type from the top-level error only (its ErrorToFailure
// switches on err's type), so Temporal retries a wrapped one (#8529).
func final(err error, attempt int32, policy *temporal.RetryPolicy) bool {
	app, _ := err.(*temporal.ApplicationError)
	if app != nil && app.NonRetryable() {
		return true
	}
	if policy == nil {
		return false
	}
	if policy.MaximumAttempts > 0 && attempt >= policy.MaximumAttempts {
		return true
	}
	return app != nil && slices.Contains(policy.NonRetryableErrorTypes, app.Type())
}

func (r *record) failed(ctx context.Context, msg, id string, err error) {
	if r.failures != nil {
		r.failures.Add(ctx, 1)
	}
	slog.WarnContext(ctx, msg, "operation_id", id, "error", err.Error())
}

// inputOf is the activity's Input among its arguments, as the SDK decoded them.
func inputOf(args []any) (Input, bool) {
	if len(args) == 0 {
		return Input{}, false
	}
	switch v := args[0].(type) {
	case Input:
		return v, true
	case *Input:
		return *v, v != nil
	}
	return Input{}, false
}

// subjectParams are the path parameters that name an entity, first first, with the kind
// the subject names it by.
var subjectParams = [][2]string{{"item_id", "item"}, {"lot_id", "lot"}, {"unit_id", "unit"}, {"kind_id", "item_kind"}}

// subject is the first entity the input names: a path parameter, else the body's kind (a
// new item's kind_id, a new kind's id, a repoint's gone entity).
func subject(in Input) string {
	for _, p := range subjectParams {
		if v := in.Params[p[0]]; v != "" {
			return p[1] + ":" + v
		}
	}
	var body struct {
		ID     string `json:"id"`
		KindID string `json:"kind_id"`
		Kind   string `json:"kind"`
		Gone   string `json:"gone"`
	}
	if json.Unmarshal(in.Body, &body) != nil {
		return ""
	}
	switch {
	case body.KindID != "":
		return "item_kind:" + body.KindID
	case body.Kind != "" && body.Gone != "":
		return body.Kind + ":" + body.Gone
	case body.ID != "":
		return "item_kind:" + body.ID
	}
	return ""
}
