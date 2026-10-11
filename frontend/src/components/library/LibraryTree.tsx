import { useEffect, useMemo, useRef, useState, type KeyboardEvent } from 'react'
import type { LibraryFolderView } from '../../api/types'
import { ancestry } from '../../lib/libraryPath'

/** The top level's key: a folder id, or this for the library's root. */
const TOP = 'top'
type Key = number | typeof TOP

interface Props {
  /** Bambuddy's folders, flattened depth first as the listing sends them. */
  folders: LibraryFolderView[]
  selected: number | null
  onSelect: (id: number | null) => void
}

function fileCountLabel(folder: LibraryFolderView): string | undefined {
  // #975 — "MakerWorld, 49 files", not "MakerWorld49" run together.
  if (!folder.file_count) return undefined
  return `${folder.name}, ${folder.file_count} ${folder.file_count === 1 ? 'file' : 'files'}`
}

/**
 * #2165 — Bambuddy's folders as a tree (WAI-ARIA tree view): it opens on the selected
 * folder's ancestors, the rest collapsed. Up and Down move between the visible rows,
 * Right opens a folder (or moves into it), Left closes it (or moves to its parent),
 * Home and End go to the first and last, and Enter or Space selects.
 */
export function LibraryTree({ folders, selected, onSelect }: Props) {
  const children = useMemo(() => {
    const map = new Map<number | null, LibraryFolderView[]>()
    for (const folder of folders) {
      const parent = folder.parent_id ?? null
      map.set(parent, [...(map.get(parent) ?? []), folder])
    }
    return map
  }, [folders])
  const hasChildren = (id: number) => (children.get(id)?.length ?? 0) > 0

  const [expanded, setExpanded] = useState<Set<number>>(() => new Set())
  // The selected folder's ancestors open whenever it changes, as a deep link or Back
  // lands on it; whatever else was opened stays open.
  const selectedChain = ancestry(folders, selected)
    .slice(0, -1)
    .map((folder) => folder.id)
    .join(',')
  useEffect(() => {
    if (selectedChain === '') return
    setExpanded((now) => {
      const ids = selectedChain.split(',').map(Number)
      if (ids.every((id) => now.has(id))) return now
      return new Set([...now, ...ids])
    })
  }, [selectedChain])

  const visible = useMemo(() => {
    const rows: { folder: LibraryFolderView; level: number }[] = []
    const walk = (parent: number | null, level: number) => {
      for (const folder of children.get(parent) ?? []) {
        rows.push({ folder, level })
        if (expanded.has(folder.id)) walk(folder.id, level + 1)
      }
    }
    walk(null, 1)
    return rows
  }, [children, expanded])

  const keys: Key[] = [TOP, ...visible.map((row) => row.folder.id)]
  const selectedKey: Key = selected ?? TOP
  const [focused, setFocused] = useState<Key>(selectedKey)
  const tabStop = keys.includes(focused) ? focused : keys.includes(selectedKey) ? selectedKey : TOP
  const treeRef = useRef<HTMLUListElement>(null)

  function focusKey(key: Key) {
    setFocused(key)
    treeRef.current?.querySelector<HTMLElement>(`[data-key="${key}"]`)?.focus()
  }

  function toggle(id: number, open?: boolean) {
    setExpanded((now) => {
      const next = new Set(now)
      if (open ?? !now.has(id)) next.add(id)
      else next.delete(id)
      return next
    })
  }

  function onKeyDown(event: KeyboardEvent<HTMLElement>, key: Key) {
    const index = keys.indexOf(key)
    const folder = key === TOP ? undefined : folders.find((row) => row.id === key)
    switch (event.key) {
      case 'ArrowDown':
        if (index < keys.length - 1) focusKey(keys[index + 1]!)
        break
      case 'ArrowUp':
        if (index > 0) focusKey(keys[index - 1]!)
        break
      case 'Home':
        focusKey(keys[0]!)
        break
      case 'End':
        focusKey(keys[keys.length - 1]!)
        break
      case 'ArrowRight':
        if (folder && hasChildren(folder.id)) {
          if (!expanded.has(folder.id)) toggle(folder.id, true)
          else focusKey(children.get(folder.id)![0]!.id)
        }
        break
      case 'ArrowLeft':
        if (folder && expanded.has(folder.id)) toggle(folder.id, false)
        else if (folder) focusKey(folder.parent_id ?? TOP)
        break
      case 'Enter':
      case ' ':
        onSelect(key === TOP ? null : key)
        break
      default:
        return
    }
    event.preventDefault()
    event.stopPropagation()
  }

  const rowClass = (key: Key) =>
    `flex w-full min-w-0 items-center gap-1 rounded-[6px] py-1 pr-2 text-left outline-none focus-visible:ring-2 focus-visible:ring-accent ${
      selectedKey === key ? 'bg-accent/8 text-ink' : 'text-muted hover:text-ink'
    }`

  function renderFolder(folder: LibraryFolderView, level: number) {
    const open = expanded.has(folder.id)
    const branch = hasChildren(folder.id)
    return (
      <li
        key={folder.id}
        data-key={folder.id}
        role="treeitem"
        aria-level={level}
        aria-selected={selectedKey === folder.id}
        aria-expanded={branch ? open : undefined}
        aria-label={fileCountLabel(folder)}
        tabIndex={tabStop === folder.id ? 0 : -1}
        data-testid={`library-folder-${folder.id}`}
        onKeyDown={(event) => onKeyDown(event, folder.id)}
        onFocus={(event) => {
          if (event.target === event.currentTarget) setFocused(folder.id)
        }}
        onClick={(event) => {
          event.stopPropagation()
          setFocused(folder.id)
          onSelect(folder.id)
        }}
        className="outline-none [&:focus-visible>div]:ring-2 [&:focus-visible>div]:ring-accent"
      >
        <div className={rowClass(folder.id)} style={{ paddingLeft: `${4 + (level - 1) * 14}px` }}>
          {branch ? (
            <span
              aria-hidden
              data-testid={`library-folder-toggle-${folder.id}`}
              onClick={(event) => {
                event.stopPropagation()
                toggle(folder.id)
              }}
              className="grid size-5 shrink-0 cursor-pointer place-items-center rounded text-faint hover:text-ink"
            >
              <svg viewBox="0 0 12 12" className={`size-3 transition-transform ${open ? 'rotate-90' : ''}`}>
                <path d="M4 2.5 7.5 6 4 9.5" fill="none" stroke="currentColor" strokeWidth="1.5" />
              </svg>
            </span>
          ) : (
            <span aria-hidden className="size-5 shrink-0" />
          )}
          <span className="truncate">{folder.name}</span>
          {folder.file_count ? <span className="sb-num ml-auto pl-1 text-faint">{folder.file_count}</span> : null}
        </div>
        {branch && open && (
          <ul role="group" className="mt-0.5 space-y-0.5">
            {(children.get(folder.id) ?? []).map((child) => renderFolder(child, level + 1))}
          </ul>
        )}
      </li>
    )
  }

  return (
    <ul ref={treeRef} role="tree" aria-label="Library folders" className="space-y-0.5 text-[13px]">
      <li
        data-key={TOP}
        role="treeitem"
        aria-level={1}
        aria-selected={selectedKey === TOP}
        tabIndex={tabStop === TOP ? 0 : -1}
        data-testid="library-folder-root"
        onKeyDown={(event) => onKeyDown(event, TOP)}
        onFocus={(event) => {
          if (event.target === event.currentTarget) setFocused(TOP)
        }}
        onClick={() => {
          setFocused(TOP)
          onSelect(null)
        }}
        className="outline-none [&:focus-visible>div]:ring-2 [&:focus-visible>div]:ring-accent"
      >
        <div className={rowClass(TOP)} style={{ paddingLeft: '4px' }}>
          <span aria-hidden className="size-5 shrink-0" />
          Top level
        </div>
      </li>
      {(children.get(null) ?? []).map((folder) => renderFolder(folder, 1))}
    </ul>
  )
}
