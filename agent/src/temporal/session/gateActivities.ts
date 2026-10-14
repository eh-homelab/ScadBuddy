import type { Sql } from 'postgres'
import { denyInput, grantInput, openInput, resolveInput } from '../../gate/store.js'
import { SESSION_GATE_ACTIVITIES } from './gate.js'

// The gate's writers (gate/store.ts) as activities under the names SessionGate schedules,
// which are agent-durable's (gate/activities.py), so one history reads the same under
// either half. Not registered on the agent-tools worker: the Python worker serves these
// names on its own queue today, and phase 5's AgentSession picks the queue it runs them on
// (SessionGateOptions.taskQueue). Arguments are camelCase, as store.ts's types are.
export function sessionGateActivities(sql: Sql) {
  return {
    [SESSION_GATE_ACTIVITIES.open]: (args: Parameters<typeof openInput>[1]) => openInput(sql, args),
    [SESSION_GATE_ACTIVITIES.grant]: (args: Parameters<typeof grantInput>[1]) => grantInput(sql, args),
    [SESSION_GATE_ACTIVITIES.deny]: (args: Parameters<typeof denyInput>[1]) => denyInput(sql, args),
    [SESSION_GATE_ACTIVITIES.resolve]: (args: Parameters<typeof resolveInput>[1]) => resolveInput(sql, args),
  }
}
