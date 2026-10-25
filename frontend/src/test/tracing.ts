// frontend/src/test/tracing.ts
import { context, propagation, trace } from '@opentelemetry/api'
import {
  InMemorySpanExporter,
  SimpleSpanProcessor,
  StackContextManager,
  TracerProvider,
} from '@opentelemetry/sdk-trace'

/**
 * A registered provider whose spans land in `exporter`, for tests of code that makes
 * spans. `uninstall` (call it in `afterEach` or a `finally`) puts the API back to its
 * no-op state, so the next test starts untraced.
 */
export function installTestTracing(): { exporter: InMemorySpanExporter; uninstall: () => void } {
  const exporter = new InMemorySpanExporter()
  const provider = new TracerProvider({ spanProcessors: [new SimpleSpanProcessor({ exporter })] })
  context.setGlobalContextManager(new StackContextManager().enable())
  trace.setGlobalTracerProvider(provider)
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
