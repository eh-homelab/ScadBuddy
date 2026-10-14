import { getQuickJS, type QuickJSContext, type QuickJSDeferredPromise, type QuickJSHandle, type QuickJSRuntime } from 'quickjs-emscripten'
import { type AgentOptions, checkMeta, impureMeta, type ScriptMeta, splitScript, WORKFLOW_MAX_ITEMS } from './workflowScript.js'

// Runs a Workflow script (workflowScript.ts) in QuickJS compiled to WebAssembly: an
// interpreter of its own, whose only way out is the functions given to it here. The
// script is model-written code, so it never runs in the service's own JavaScript (Node's
// vm, Temporal's workflow sandbox included, is not a boundary: a host object's
// constructor reaches the host's Function, and through it the process). Inside it there is
// no process, require, fetch, file or clock: `new Date()`, `Date.now()` and
// `Math.random()` throw, as Claude Code's do, so a re-run makes the same calls in the same
// order, and its journal (activities.ts `wf_run`) answers the ones already made.
//
// Its memory is capped (MEMORY_BYTES) and so is every stretch of it that runs without
// awaiting (SLICE_MS): an endless loop is stopped, never the service.

export const MEMORY_BYTES = 64 * 1024 * 1024
export const SLICE_MS = 5_000

/** What a script reaches outside itself. */
export type ScriptHost = {
  /** One agent() call: its result, or null when the agent failed or was given up. */
  agent(prompt: string, opts: AgentOptions): Promise<unknown>
  phase(title: string): void
  log(message: string): void
  /** The body of the script at `scriptPath` (workflow({scriptPath})), meta already read. */
  script(scriptPath: string): Promise<string>
}

export type ScriptOutcome = { ok: true; result: unknown } | { ok: false; error: string }

const PRELUDE = `
"use strict";
(() => {
  const host = { agent: __agent, phase: __phase, log: __log, script: __script };
  for (const k of ['__agent', '__phase', '__log', '__script']) delete globalThis[k];
  const unavailable = (what) => new Error(what + ' is unavailable in workflow scripts (it would break resume); pass timestamps in via args');
  const RealDate = Date;
  class ScriptDate extends RealDate {
    constructor(...a) { if (!a.length) throw unavailable('new Date()'); super(...a); }
    static now() { throw unavailable('Date.now()'); }
  }
  globalThis.Date = ScriptDate;
  Math.random = () => { throw unavailable('Math.random()'); };
  const MAX = ${WORKFLOW_MAX_ITEMS};
  const items = (what, list) => {
    if (!Array.isArray(list)) throw new TypeError(what + ' takes an array');
    if (list.length > MAX) throw new RangeError(what + ' takes at most ' + MAX + ' items; got ' + list.length);
  };
  globalThis.agent = async (prompt, opts) => {
    if (typeof prompt !== 'string') throw new TypeError('agent(prompt, opts): prompt must be a string');
    const out = await host.agent(prompt, JSON.stringify(opts === undefined ? {} : opts));
    return out === undefined ? null : JSON.parse(out);
  };
  globalThis.parallel = async (thunks) => {
    items('parallel()', thunks);
    return Promise.all(thunks.map((t) => Promise.resolve().then(t).then((v) => v, () => null)));
  };
  globalThis.pipeline = async (list, ...stages) => {
    items('pipeline()', list);
    return Promise.all(list.map(async (item, index) => {
      let value = item;
      for (const stage of stages) {
        try { value = await stage(value, item, index); } catch (e) { return null; }
      }
      return value;
    }));
  };
  globalThis.phase = (title) => host.phase(String(title));
  globalThis.log = (message) => host.log(String(message));
  globalThis.budget = Object.freeze({ total: null, spent: () => 0, remaining: () => Infinity });
  let nested = false;
  globalThis.workflow = async (ref, args) => {
    if (nested) throw new Error('workflow() inside a child workflow: nesting is one level only');
    if (typeof ref === 'string') throw new Error('No saved workflow named "' + ref + '": this session has no saved workflows; pass {scriptPath}');
    if (!ref || typeof ref.scriptPath !== 'string') throw new TypeError('workflow(nameOrRef): give a name or {scriptPath}');
    const body = await host.script(ref.scriptPath);
    const run = new Function('args', '"use strict"; return (async () => {\\n' + body + '\\n})()');
    nested = true;
    try { return await run(args); } finally { nested = false; }
  };
})();
`

type Live = { rt: QuickJSRuntime; vm: QuickJSContext; deadline: number; gone: boolean }

function enter(live: Live): void {
  live.deadline = Date.now() + SLICE_MS
}

/** Runs pending guest jobs, as a host promise settles. */
function drain(live: Live): void {
  if (live.gone) return
  enter(live)
  const ran = live.rt.executePendingJobs()
  if (ran.error) ran.error.dispose()
}

function errorText(vm: QuickJSContext, handle: QuickJSHandle): string {
  const e = vm.dump(handle) as { name?: string; message?: string } | string
  handle.dispose()
  if (typeof e === 'string') return e
  return e && typeof e === 'object' && e.message !== undefined ? `${e.name ?? 'Error'}: ${e.message}` : JSON.stringify(e)
}

async function withVm<T>(use: (live: Live) => Promise<T>): Promise<T> {
  const QJS = await getQuickJS()
  const rt = QJS.newRuntime()
  rt.setMemoryLimit(MEMORY_BYTES)
  rt.setMaxStackSize(1024 * 1024)
  const live: Live = { rt, vm: rt.newContext(), deadline: Date.now() + SLICE_MS, gone: false }
  rt.setInterruptHandler(() => Date.now() > live.deadline)
  try {
    return await use(live)
  } finally {
    live.gone = true
    try {
      live.vm.dispose()
      rt.dispose()
    } catch {
      // A promise still held by the guest: the runtime goes with the activity's process.
    }
  }
}

/** The script's meta and body, checked: meta a pure literal, the body JavaScript that parses. Throws with what is wrong. */
export async function readScript(script: string): Promise<{ meta: ScriptMeta; body: string }> {
  const { metaSource, body } = splitScript(script)
  const impure = impureMeta(metaSource)
  if (impure) throw new Error(impure)
  return withVm(async ({ vm }) => {
    const meta = vm.evalCode(`(${metaSource})`)
    if (meta.error) throw new Error(`meta does not parse: ${errorText(vm, meta.error)}`)
    const value = vm.dump(meta.value) as unknown
    meta.value.dispose()
    // Compiled, not run: a syntax error is reported now, as Claude Code reports it.
    const parsed = vm.evalCode(`(async () => {\n${body}\n})`)
    if (parsed.error) throw new Error(`The script does not parse: ${errorText(vm, parsed.error)}`)
    parsed.value.dispose()
    return { meta: checkMeta(value), body }
  })
}

/** Runs a script's body with `args` until it returns or throws, or `signal` aborts (then it throws). */
export async function runScript(body: string, args: unknown, host: ScriptHost, signal: AbortSignal): Promise<ScriptOutcome> {
  return withVm(async (live) => {
    const { vm } = live
    const pending = new Set<QuickJSDeferredPromise>()
    // A host function that answers later: a guest promise, settled with a JSON string (or undefined).
    const later = (name: string, work: (...a: string[]) => Promise<string | undefined>) => {
      const fn = vm.newFunction(name, (...handles) => {
        const strings = handles.map((h) => vm.getString(h))
        const deferred = vm.newPromise()
        pending.add(deferred)
        work(...strings).then(
          (out) => {
            if (live.gone) return
            const h = out === undefined ? vm.undefined : vm.newString(out)
            deferred.resolve(h)
            if (out !== undefined) h.dispose()
          },
          (err: unknown) => {
            if (live.gone) return
            const h = vm.newError(err instanceof Error ? err.message : String(err))
            deferred.reject(h)
            h.dispose()
          },
        )
        void deferred.settled.then(() => {
          pending.delete(deferred)
          drain(live)
        })
        return deferred.handle
      })
      vm.setProp(vm.global, name, fn)
      fn.dispose()
    }
    const now = (name: string, work: (a: string) => void) => {
      const fn = vm.newFunction(name, (h) => {
        work(vm.getString(h))
        return vm.undefined
      })
      vm.setProp(vm.global, name, fn)
      fn.dispose()
    }
    later('__agent', async (prompt, opts) => {
      const out = await host.agent(prompt!, JSON.parse(opts!) as AgentOptions)
      return JSON.stringify(out ?? null)
    })
    later('__script', async (scriptPath) => host.script(scriptPath!))
    now('__phase', (t) => host.phase(t))
    now('__log', (m) => host.log(m))
    enter(live)
    const prelude = vm.evalCode(PRELUDE)
    if (prelude.error) throw new Error(`the workflow prelude failed: ${errorText(vm, prelude.error)}`)
    prelude.value.dispose()
    const argsJson = JSON.stringify(args ?? null)
    enter(live)
    const started = vm.evalCode(
      `(async (args) => { const value = await (async () => {\n${body}\n})(); return value === undefined ? undefined : JSON.stringify(value); })(JSON.parse(${JSON.stringify(argsJson)}))`,
    )
    if (started.error) return { ok: false, error: errorText(vm, started.error) }
    const aborted = new Promise<never>((_, reject) => {
      const stop = () => reject(signal.reason instanceof Error ? signal.reason : new Error('the workflow was stopped'))
      if (signal.aborted) stop()
      else signal.addEventListener('abort', stop, { once: true })
    })
    const settled = vm.resolvePromise(started.value)
    started.value.dispose()
    drain(live)
    const result = await Promise.race([settled, aborted])
    if (result.error) return { ok: false, error: errorText(vm, result.error) }
    const out = vm.dump(result.value) as string | undefined
    result.value.dispose()
    return { ok: true, result: out === undefined ? null : (JSON.parse(out) as unknown) }
  })
}
