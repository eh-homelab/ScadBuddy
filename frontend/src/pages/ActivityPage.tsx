import { useCallback, useEffect, useRef, useState } from "react";
import { useSearchParams } from "react-router";
import { Loader2Icon } from "lucide-react";
import { api } from "../api/client";
import type {
  Activity,
  ActivityFacets,
  ActivityPage as ActivityPageData,
} from "../api/types";
import { ActivityDrawer } from "../components/activity/ActivityDrawer";
import { ActivityFilters } from "../components/activity/ActivityFilters";
import { ActivityTable } from "../components/activity/ActivityTable";
import { sinceTime } from "../components/activity/labels";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import {
  parseActivityQuery,
  toActivityParams,
  type ActivityQuery,
} from "../lib/activityQuery";
import { useAsync } from "../lib/useAsync";
import { getRealtime } from "../lib/realtime";

const PAGE = 50;
/** The API's largest page; a live re-read of what is loaded stays within it. */
const MAX_LOADED = 200;
/** The facets are an aggregate: a burst of changes re-reads them at most this often. */
const FACETS_EVERY_MS = 3000;
const LIST_DEBOUNCE_MS = 500;
const LIST_MAX_WAIT_MS = 2000;

/** The query as the API takes it: `since` is a time, the URL keeps its token. */
function wire(query: ActivityQuery): ActivityQuery {
  return { ...query, since: sinceTime(query.since) };
}

export function ActivityPage() {
  const [params, setParams] = useSearchParams();
  const query = parseActivityQuery(params);
  const key = toActivityParams(query).toString();
  // How many rows to hold for this query. A live re-read asks for that many, so rows
  // already loaded stay current and a new row cannot open a gap before the next page.
  const want = useRef({ key, count: PAGE });
  // Pages appended past the cap, by cursor. While any are held the list is not re-read
  // (a new row would open a gap before them); the facets stay live.
  const [older, setOlder] = useState<{
    key: string;
    items: Activity[];
    cursor: string | null;
  } | null>(null);
  const olderRef = useRef(older);
  const dataRef = useRef<{ key: string; data: ActivityPageData } | undefined>(
    undefined,
  );
  const gate = useRef<{
    since: string | null;
    loaded: boolean;
    last: number;
    pending: Promise<ActivityFacets> | null;
    timer: ReturnType<typeof setTimeout> | undefined;
  }>({ since: null, loaded: false, last: 0, pending: null, timer: undefined });
  const sinceRef = useRef(query.since);
  useEffect(() => {
    sinceRef.current = query.since;
  });
  // A query change drops the pages appended under the old one.
  const [seenKey, setSeenKey] = useState(key);
  if (seenKey !== key) {
    setSeenKey(key);
    setOlder(null);
  }

  const facets = useAsync(
    () => {
      const g = gate.current;
      const read = () =>
        api
          .activityFacets(sinceTime(sinceRef.current) || undefined)
          .then((f) => {
            g.loaded = true;
            return f;
          });
      // Only a live re-read is throttled; a first read (until one lands: a mount's effect
      // can run twice), or one for another `since`, is not.
      if (g.since !== query.since || !g.loaded) {
        g.since = query.since;
        clearTimeout(g.timer);
        g.pending = null;
        g.last = Date.now();
        return read();
      }
      const wait = g.last + FACETS_EVERY_MS - Date.now();
      if (wait <= 0) {
        g.last = Date.now();
        return read();
      }
      g.pending ??= new Promise<ActivityFacets>((resolve, reject) => {
        g.timer = setTimeout(() => {
          g.pending = null;
          g.last = Date.now();
          read().then(resolve, reject);
        }, wait);
      });
      return g.pending;
    },
    [query.since],
    ["activity"],
  );
  const list = useAsync(() => {
    if (want.current.key !== key) {
      want.current = { key, count: PAGE };
      olderRef.current = null;
    }
    const kept = dataRef.current;
    if (olderRef.current?.key === key && kept?.key === key)
      return Promise.resolve(kept.data);
    return api.listActivity(wire(query), { limit: want.current.count });
  }, [key]);
  // The list re-reads on `activity` signals, but a burst (every render writes several) is
  // read once: signals within LIST_DEBOUNCE_MS merge, and none waits past LIST_MAX_WAIT_MS.
  const { refresh: refreshList, staleFor: listStaleFor } = list;
  useEffect(() => {
    let timer: ReturnType<typeof setTimeout> | undefined;
    let first = 0;
    const schedule = () => {
      clearTimeout(timer);
      const now = Date.now();
      if (first === 0) first = now;
      timer = setTimeout(
        () => {
          first = 0;
          refreshList();
        },
        Math.max(0, Math.min(LIST_DEBOUNCE_MS, first + LIST_MAX_WAIT_MS - now)),
      );
    };
    const stop = getRealtime().subscribe("activity", (_signal, confirmation) => {
      // The confirmation is news only if something passed since the first read (#1039).
      if (!confirmation) return schedule();
      void listStaleFor("activity", confirmation).then((stale) => {
        if (stale) schedule();
      });
    });
    return () => {
      stop();
      clearTimeout(timer);
    };
  }, [refreshList, listStaleFor]);
  const [loadingMore, setLoadingMore] = useState(false);
  const [moreFailed, setMoreFailed] = useState<string>();
  const [open, setOpen] = useState<string | null>(null);

  useEffect(() => {
    dataRef.current = list.data ? { key, data: list.data } : undefined;
  }, [list.data, key]);
  useEffect(() => {
    const g = gate.current;
    return () => {
      clearTimeout(g.timer);
      g.pending = null;
    };
  }, []);

  const onChange = useCallback(
    (next: ActivityQuery, options?: { typing?: boolean }) =>
      setParams(toActivityParams(next), { replace: options?.typing === true }),
    [setParams],
  );

  const held = older?.key === key ? older : null;
  const base = list.data?.items ?? [];
  const items = [
    ...base,
    ...(held?.items ?? []).filter((i) => !base.some((b) => b.id === i.id)),
  ];
  const hasMore = held ? held.cursor !== null : list.data?.next_cursor != null;
  const capped = base.length >= MAX_LOADED;

  useEffect(() => {
    setLoadingMore(false);
  }, [list.data]);

  function keepOlder(next: typeof older) {
    olderRef.current = next;
    setOlder(next);
  }

  function loadMore() {
    setLoadingMore(true);
    setMoreFailed(undefined);
    if (!capped) {
      const previous = want.current.count;
      want.current = { key, count: Math.min(MAX_LOADED, base.length + PAGE) };
      list.refresh(undefined, () => {
        want.current = { key, count: previous };
        setLoadingMore(false);
        setMoreFailed(key);
      });
      return;
    }
    const cursor = held ? held.cursor : (list.data?.next_cursor ?? null);
    if (!cursor) return;
    // Freeze before asking, so a live re-read cannot move the base past the cursor.
    const before = held;
    keepOlder({ key, items: before?.items ?? [], cursor });
    api.listActivity(wire(query), { cursor, limit: PAGE }).then(
      (page) => {
        keepOlder({
          key,
          items: [...(before?.items ?? []), ...page.items],
          cursor: page.next_cursor,
        });
        setLoadingMore(false);
      },
      () => {
        keepOlder(before);
        setLoadingMore(false);
        setMoreFailed(key);
      },
    );
  }

  function showLatest() {
    want.current = { key, count: PAGE };
    keepOlder(null);
    list.refresh();
  }

  return (
    // #3747 — the page is its own scroll container, as every page is: the shell's
    // <main> clips, so a page that does not scroll cannot reach what is below the fold.
    <div className="h-full overflow-y-auto" data-testid="activity-page">
      <div className="mx-auto flex max-w-6xl flex-col gap-4 px-4 py-6">
        <h1 className="text-xl font-semibold tracking-tight">Activity</h1>
        <ActivityFilters query={query} facets={facets.data} onChange={onChange} />
        {list.error ? (
          <p role="alert" className="text-[13px] text-warn">
            Could not load activity: {list.error.message}
          </p>
        ) : list.loading ? (
          <div
            role="status"
            aria-label="Loading activity"
            className="flex flex-col gap-2"
          >
            {Array.from({ length: 5 }, (_, i) => (
              <Skeleton key={i} className="h-9 w-full" />
            ))}
          </div>
        ) : items.length === 0 ? (
          <p className="rounded-xl border border-dashed px-4 py-8 text-center text-sm text-muted-foreground">
            No activity matches.
          </p>
        ) : (
          <ActivityTable items={items} facets={facets.data} onOpen={setOpen} />
        )}
        {moreFailed === key && (
          <p role="alert" className="text-[13px] text-warn">
            Could not load more. Try again.
          </p>
        )}
        {held && (
          <p className="flex flex-wrap items-center gap-2 text-sm text-muted-foreground">
            Live updates paused, showing older activity.
            <Button size="sm" variant="outline" onClick={showLatest}>
              Show latest
            </Button>
          </p>
        )}
        {hasMore && (
          <div className="flex justify-center">
            <Button variant="outline" onClick={loadMore} disabled={loadingMore}>
              {loadingMore && <Loader2Icon className="animate-spin" />}
              Load more
            </Button>
          </div>
        )}
        {/* Keyed: each row's drawer captures its own opener for focus return. */}
        {open && <ActivityDrawer key={open} id={open} onClose={() => setOpen(null)} />}
      </div>
    </div>
  );
}
