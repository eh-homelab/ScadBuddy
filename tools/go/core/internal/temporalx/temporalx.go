// Package temporalx is core's Temporal client and worker settings, read from the
// environment as the Python API reads its own (core/config.py, workflows/client.py), and
// the operations read's question to Temporal (store.Temporal).
package temporalx

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"time"

	"go.opentelemetry.io/otel/propagation"
	"go.opentelemetry.io/otel/trace"
	enumspb "go.temporal.io/api/enums/v1"
	"go.temporal.io/api/serviceerror"
	"go.temporal.io/sdk/client"
	temporalotel "go.temporal.io/sdk/contrib/opentelemetry"
	"go.temporal.io/sdk/interceptor"
	"go.temporal.io/sdk/log"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/inventory/store"
)

const (
	// DefaultNamespace is core/config.py DEFAULT_TEMPORAL_NAMESPACE.
	DefaultNamespace = "scadbuddy"
	// DefaultInventoryQueue is the inventory activities' task queue (design §6.1a).
	DefaultInventoryQueue = "inventory"
	// DefaultFilamentPresetsQueue is the task queue of core's LibraryRun workflow
	// (#7204, plan ruling P21; #8489 U2: the preset writes and the unknown-tray resolve).
	DefaultFilamentPresetsQueue = "filament-presets"
	// DefaultLibraryQueue is core/settings.py temporal_task_queue_library: the Python queue
	// the preset write activities run on.
	DefaultLibraryQueue = "library"
)

// Config is where core's Temporal is: SCADBUDDY_TEMPORAL_ADDRESS (host:port, no TLS, as the
// Python client uses none), SCADBUDDY_TEMPORAL_NAMESPACE,
// SCADBUDDY_TEMPORAL_TASK_QUEUE_INVENTORY, SCADBUDDY_TEMPORAL_TASK_QUEUE_FILAMENT_PRESETS and
// SCADBUDDY_TEMPORAL_TASK_QUEUE_LIBRARY (Python's own variable for the library queue).
type Config struct {
	Address, Namespace, InventoryQueue, FilamentPresetsQueue, LibraryQueue string
}

// FromEnv reads the Config. An empty address is an error naming its variable: the start
// stops without it.
func FromEnv(env func(string) string) (Config, error) {
	c := Config{
		Address:        env("SCADBUDDY_TEMPORAL_ADDRESS"),
		Namespace:      env("SCADBUDDY_TEMPORAL_NAMESPACE"),
		InventoryQueue: env("SCADBUDDY_TEMPORAL_TASK_QUEUE_INVENTORY"),

		FilamentPresetsQueue: env("SCADBUDDY_TEMPORAL_TASK_QUEUE_FILAMENT_PRESETS"),
		LibraryQueue:         env("SCADBUDDY_TEMPORAL_TASK_QUEUE_LIBRARY"),
	}
	if c.Address == "" {
		return Config{}, errors.New("SCADBUDDY_TEMPORAL_ADDRESS is required when SCADBUDDY_CORE_AREAS names inventory")
	}
	if c.Namespace == "" {
		c.Namespace = DefaultNamespace
	}
	if c.InventoryQueue == "" {
		c.InventoryQueue = DefaultInventoryQueue
	}
	if c.FilamentPresetsQueue == "" {
		c.FilamentPresetsQueue = DefaultFilamentPresetsQueue
	}
	if c.LibraryQueue == "" {
		c.LibraryQueue = DefaultLibraryQueue
	}
	return c, nil
}

// Client is a lazy client of c: it connects on its first call, so core starts, and serves
// its HTTP routes, while Temporal is out of reach. With tracer (nil for none), each
// activity a worker of it runs is a span that continues the caller's trace. Payloads use the
// SDK's default JSON converter, which reads Python's pydantic_data_converter's json/plain
// payloads.
func Client(c Config, tracer trace.TracerProvider) (client.Client, error) {
	opts := client.Options{
		HostPort:  c.Address,
		Namespace: c.Namespace,
		Logger:    log.NewStructuredLogger(slog.Default()),
	}
	if tracer != nil {
		tracing, err := temporalotel.NewTracingInterceptor(temporalotel.TracerOptions{
			Tracer:            tracer.Tracer("go.temporal.io/sdk"),
			TextMapPropagator: propagation.TraceContext{},
		})
		if err != nil {
			return nil, err
		}
		opts.Interceptors = []interceptor.ClientInterceptor{tracing}
	}
	return client.NewLazyClient(opts)
}

// Checker answers the operations read's question (store.Temporal) with a client.
type Checker struct{ Client client.Client }

// Closed tells the state of the activity id (a standalone one, store.ActivityID) or of the workflow
// workflowID (one that runs it as a step). Failed, timed out, cancelled or terminated is
// store.Failed, with the last failure's message; completed is store.Completed. An id
// Temporal does not know is store.Failed too: no attempt of it will run.
func (c Checker) Closed(ctx context.Context, id, workflowID string) (store.Closure, error) {
	ctx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	var notFound *serviceerror.NotFound
	if workflowID != "" {
		d, err := c.Client.DescribeWorkflowExecution(ctx, workflowID, "")
		if errors.As(err, &notFound) {
			return store.Closure{State: store.Failed, Detail: "Temporal has no workflow " + workflowID}, nil
		}
		if err != nil {
			return store.Closure{}, err
		}
		info := d.GetWorkflowExecutionInfo()
		at := info.GetCloseTime().AsTime()
		switch status := info.GetStatus(); status {
		case enumspb.WORKFLOW_EXECUTION_STATUS_COMPLETED:
			return store.Closure{State: store.Completed, At: at}, nil
		case enumspb.WORKFLOW_EXECUTION_STATUS_FAILED, enumspb.WORKFLOW_EXECUTION_STATUS_TIMED_OUT,
			enumspb.WORKFLOW_EXECUTION_STATUS_CANCELED, enumspb.WORKFLOW_EXECUTION_STATUS_TERMINATED:
			return store.Closure{State: store.Failed, At: at,
				Detail: fmt.Sprintf("the workflow %s ended %s", workflowID, status)}, nil
		}
		return store.Closure{State: store.Open}, nil
	}
	d, err := c.Client.GetActivityHandle(client.GetActivityHandleOptions{ActivityID: id}).
		Describe(ctx, client.DescribeActivityOptions{})
	if errors.As(err, &notFound) {
		return store.Closure{State: store.Failed, Detail: "Temporal has no activity " + id}, nil
	}
	if err != nil {
		return store.Closure{}, err
	}
	switch d.Status {
	case enumspb.ACTIVITY_EXECUTION_STATUS_COMPLETED:
		return store.Closure{State: store.Completed, At: d.CloseTime}, nil
	case enumspb.ACTIVITY_EXECUTION_STATUS_FAILED, enumspb.ACTIVITY_EXECUTION_STATUS_TIMED_OUT,
		enumspb.ACTIVITY_EXECUTION_STATUS_CANCELED, enumspb.ACTIVITY_EXECUTION_STATUS_TERMINATED:
		detail := d.RawResponse.GetInfo().GetLastFailure().GetMessage()
		if detail == "" {
			detail = "the activity ended " + d.Status.String()
		}
		return store.Closure{State: store.Failed, Detail: detail, At: d.CloseTime}, nil
	}
	return store.Closure{State: store.Open}, nil
}
