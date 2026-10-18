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
