-- #988: the first caller's trace context, so a request that coalesces onto this row
-- can link to the render it joined, and the reconciler can start a late workflow in
-- the trace it belongs to. NULL when the row predates tracing, the sampler dropped
-- the request, or OTEL_SDK_DISABLED was set.
ALTER TABLE render_jobs ADD COLUMN IF NOT EXISTS traceparent text;
