// frontend/src/test/tracing.ts
import { context, propagation, trace } from '@opentelemetry/api'
import {
  InMemorySpanExporter,
  SimpleSpanProcessor,
  StackContextManager,
  WebTracerProvider,
} from '@opentelemetry/sdk-trace-web'

/**
 * A registered provider whose spans land in `exporter`, for tests of code that makes
 * spans. `uninstall` (call it in `afterEach` or a `finally`) puts the API back to its
 * no-op state, so the next test starts untraced.
 */
export function installTestTracing(): { exporter: InMemorySpanExporter; uninstall: () => void } {
  const exporter = new InMemorySpanExporter()
  const provider = new WebTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] })
  provider.register({ contextManager: new StackContextManager(), propagator: null })
  return {
    exporter,
    uninstall: () => {
      void provider.shutdown()
      trace.disable()
      context.disable()
      propagation.disable()
    },
  }
}
