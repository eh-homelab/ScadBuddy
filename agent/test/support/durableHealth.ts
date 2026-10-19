/**
 * The health port the agent-durable sidecar bound, from its log, or undefined before it
 * says. It is started on port 0 and names the port it got, so no port is picked first
 * and taken by something else before it binds (#3655). Only its own logger's line
 * counts (`logging.basicConfig`'s `LEVEL:name:message`), never another that mentions a port,
 * and only once its newline is in: a chunk can end inside the number.
 */
export function durableHealthPort(log: string): number | undefined {
  const match = /^INFO:scadbuddy_durable:health on port (\d+)\r?\n/m.exec(log)
  return match ? Number(match[1]) : undefined
}

/**
 * The error for a sidecar that did not start in time. When its port line was never read,
 * it says so: either the sidecar never got that far (a long uv sync), or a `format=` given
 * to `basicConfig` in agent-durable's `__main__.py` stops durableHealthPort matching, and
 * the start would otherwise only look slow.
 */
export function notStarted(what: string, port: number | undefined, log: string): Error {
  const unread =
    port === undefined
      ? ' (its health port line was never read: the sidecar never got that far, or durableHealthPort in ' +
        'agent/test/support/durableHealth.ts no longer matches its `INFO:scadbuddy_durable:health on port N`)'
      : ''
  return new Error(`${what} did not start${unread}: ${log}`)
}
