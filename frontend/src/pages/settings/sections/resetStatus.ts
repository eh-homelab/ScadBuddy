import type { LinksResetDone, SyncStatus } from '../../../api/filament'

/** #6845 — a kept reset has recorded the configured Bambuddy as the links' instance: show it before the next refresh. */
export function statusAfterKeptReset<S extends Pick<SyncStatus, 'instance' | 'instance_changed'>>(status: S, done: LinksResetDone): S {
  return { ...status, instance: done.configured_instance ?? status.instance, instance_changed: false }
}
