// agent/src/telemetry/sampler.ts
import { type Attributes, type Context, type Link, SpanKind } from '@opentelemetry/api'
import { ParentBasedSampler, type Sampler, SamplingDecision, type SamplingResult } from '@opentelemetry/sdk-trace'

// The default sampler (spec 2026-10-01 §6): parent-based, so the backend's and
// the browser's decisions are honoured, and every trace that starts at a
// request or a named span is kept at homelab volume. The root sampler adds the
// backend's one rule: a CLIENT span with no parent is dropped. In the agent
// that is `backendReachable`'s probe from every /healthz, which would
// otherwise be a trace of its own. OTEL_TRACES_SAMPLER, when set, replaces
// this entirely (setup.ts).

export class NoParentlessClients implements Sampler {
  shouldSample(
    _context: Context,
    _traceId: string,
    _spanName: string,
    spanKind: SpanKind,
    _attributes: Attributes,
    _links: Link[],
  ): SamplingResult {
    // Only ever asked about root spans: ParentBasedSampler answers for the rest.
    return { decision: spanKind === SpanKind.CLIENT ? SamplingDecision.NOT_RECORD : SamplingDecision.RECORD_AND_SAMPLED }
  }

  toString(): string {
    return 'NoParentlessClients'
  }
}

export const DEFAULT_SAMPLER: Sampler = new ParentBasedSampler({ root: new NoParentlessClients() })
