import type { BusEvent } from '../events/bus.js'
import { expand } from './catalog.js'

// Which resources an event changes (issue #264, "Subscriptions and
// notifications"). Kinds are the backend's (backend/scadbuddy/core/events.py);
// a kind not listed here changes nothing a client can subscribe to.
//
// `listChanged` is true when the SET of resources `resources/list` returns
// changes: it lists the catalogue's models, so a model created or deleted
// changes it. Outputs are reached through `scadbuddy://models/{slug}/outputs`
// rather than listed one by one, so an output only updates that resource.

export type Affected = { uris: string[]; listChanged: boolean }

const u = expand

function model(slug: string, ...parts: string[]): string[] {
  return parts.map((p) => u(`scadbuddy://models/{slug}${p}`, { slug }))
}

/**
 * Resources announced by NOTIFY-only kinds (the agent's `session.*`, not in
 * the backend's replay log: sessions/busEvents.ts). After the LISTEN
 * connection comes back, every subscription under these is told to re-read
 * (hub.ts `onReconnect`).
 */
export const NOTIFY_ONLY_PREFIXES: readonly string[] = ['scadbuddy://sessions']

export function affectedBy(event: BusEvent): Affected {
  const { slug } = event
  const none: Affected = { uris: [], listChanged: false }
  switch (event.kind) {
    case 'job.pending':
    case 'job.running':
    case 'job.superseded':
      return event.job_id ? { uris: [u('scadbuddy://jobs/{job_id}', { job_id: event.job_id })], listChanged: false } : none
    case 'job.done':
    case 'job.failed':
      // A settled render also replaces the model's diagnostics.
      return {
        uris: [
          ...(event.job_id ? [u('scadbuddy://jobs/{job_id}', { job_id: event.job_id })] : []),
          ...(slug ? model(slug, '/diagnostics') : []),
        ],
        listChanged: false,
      }
    case 'model.created':
    case 'model.deleted':
      return {
        uris: ['scadbuddy://models', ...(slug ? model(slug, '', '/source', '/schema', '/readme', '/thumbnail', '/versions') : [])],
        listChanged: true,
      }
    case 'model.updated':
      // Metadata, thumbnail, README, pins or upstream: the backend does not say which.
      return {
        uris: ['scadbuddy://models', ...(slug ? model(slug, '', '/readme', '/thumbnail', '/upstream') : [])],
        listChanged: false,
      }
    case 'source.changed':
      return { uris: slug ? model(slug, '/source', '/schema') : [], listChanged: false }
    case 'version.committed':
      return { uris: slug ? model(slug, '', '/versions') : [], listChanged: false }
    case 'upstream.available':
      return { uris: slug ? model(slug, '', '/upstream') : [], listChanged: false }
    case 'output.created':
    case 'output.deleted': {
      const id = event.output_id
      return {
        uris: [
          ...(slug ? model(slug, '/outputs', '/thumbnail') : []),
          ...(id ? [u('scadbuddy://outputs/{output_id}', { output_id: id }), u('scadbuddy://outputs/{output_id}/plates', { output_id: id })] : []),
        ],
        listChanged: false,
      }
    }
    case 'print.progress':
    case 'print.settled':
      return event.output_id
        ? { uris: [u('scadbuddy://print/outputs/{output_id}/progress', { output_id: event.output_id })], listChanged: false }
        : none
    case 'library.changed':
      return { uris: ['scadbuddy://libraries', ...(slug ? model(slug, '') : [])], listChanged: false }
    case 'library.removed':
      return { uris: ['scadbuddy://libraries'], listChanged: false }
    case 'font.installed':
      return { uris: ['scadbuddy://fonts'], listChanged: false }
    case 'settings.changed':
      return { uris: ['scadbuddy://settings'], listChanged: false }
    // The agent's own (sessions/busEvents.ts, #300). The list resource is told
    // only of changes that move a session in it (created, owner, status), not
    // of every streamed message.
    case 'session.started':
    case 'session.owner':
    case 'session.waiting':
    case 'session.done':
    case 'session.message': {
      const id = event.session_id
      if (!id) return none
      const one = u('scadbuddy://sessions/{session_id}', { session_id: id })
      return { uris: event.kind === 'session.message' ? [one] : [one, 'scadbuddy://sessions'], listChanged: false }
    }
    default:
      return none
  }
}
