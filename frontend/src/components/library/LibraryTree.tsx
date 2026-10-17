import { useEffect, useId, useMemo, useRef, useState, type KeyboardEvent, type ReactNode } from 'react'
import { ChevronRightIcon, FolderIcon, FolderOpenIcon, LibraryIcon } from 'lucide-react'
import type { LibraryFolderView } from '../../api/types'
import { ancestry } from '../../lib/libraryPath'
import { cn } from '@/lib/utils'
import { Badge } from '@/components/ui/badge'
import { Collapsible, CollapsibleContent } from '@/components/ui/collapsible'

/** The top level's key: a folder id, or this for the library's root. */
const TOP = 'top'
type Key = number | typeof TOP

interface Props {
  /** Bambuddy's folders, flattened depth first as the listing sends them. */
  folders: LibraryFolderView[]
  selected: number | null
  onSelect: (id: number | null) => void
  /** The tree's accessible name; a second tree on the page (a folder picker) needs its own. */
  label?: string
  /** Prefixes each row's test id, so a picker's rows are not the sidebar's. */
  testIdPrefix?: string
  /** The first row, which selects `null`; `null` leaves it out (#3650's project list). */
  rootLabel?: string | null
  /**
   * #3650 — wraps a folder's row (a context menu on right-click or long-press). The
   * tree opens it from the keyboard too: the Menu key or Shift+F10 on a row sends the
   * row a `contextmenu` event.
   */
  wrapRow?: (folder: LibraryFolderView, row: ReactNode) => ReactNode
}

/**
 * #975, #2457, #2879 — a folder's count, as its treeitem's description: "49 files". The
 * name is the visible folder name alone (WCAG 2.5.3: "click MakerWorld" works), so a
 * name ending in a number ("v2") never runs into its count ("v2 3 files"). #2724: a
 * folder linked to a Bambuddy project holds that project's files, and says which.
 */
function fileCountText(folder: LibraryFolderView): string | undefined {
  const parts: string[] = []
  if (folder.file_count) parts.push(`${folder.file_count} ${folder.file_count === 1 ? 'file' : 'files'}`)
  if (folder.project_id != null) parts.push(`project ${folder.project_name ?? folder.project_id}`)
  return parts.length ? parts.join(', ') : undefined
}

/** Opens a row's context menu where the row is, as a right-click on it would. */
function openMenuAt(row: HTMLElement | null) {
  if (!row) return
  const box = row.getBoundingClientRect()
  row.dispatchEvent(
    new MouseEvent('contextmenu', {
      bubbles: true,
      cancelable: true,
      clientX: box.left + Math.min(box.width / 2, 48),
      clientY: box.top + box.height / 2,
    }),
  )
}

/**
 * #2165 — Bambuddy's folders as a tree (WAI-ARIA tree view): it opens on the selected
 * folder's ancestors, the rest collapsed. Up and Down move between the visible rows,
 * Right opens a folder (or moves into it), Left closes it (or moves to its parent),
 * Home and End go to the first and last, and Enter or Space selects. Each branch is a
 * collapsible (#3670).
 */
export function LibraryTree({
  folders,
  selected,
  onSelect,
  label = 'Library folders',
  testIdPrefix = 'library-folder',
  rootLabel = 'Top level',
  wrapRow,
}: Props) {
  const children = useMemo(() => {
    const map = new Map<number | null, LibraryFolderView[]>()
    const known = new Set(folders.map((folder) => folder.id))
    for (const folder of folders) {
      // A parent not in the list (a project list's) makes the row a top one.
      const parent = folder.parent_id != null && known.has(folder.parent_id) ? folder.parent_id : null
      map.set(parent, [...(map.get(parent) ?? []), folder])
    }
    return map
  }, [folders])
  // The count's description id: unique per tree, whatever its test ids (#3029 review).
  const countId = useId()
  const hasChildren = (id: number) => (children.get(id)?.length ?? 0) > 0

  const [expanded, setExpanded] = useState<Set<number>>(() => new Set())
  // The selected folder and its ancestors open whenever it changes, as a deep link or
  // Back lands on it, so its subfolders show too (#3631); whatever else was opened stays
  // open.
  const selectedChain = ancestry(folders, selected)
    .map((folder) => folder.id)
    .filter(hasChildren)
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

  const withRoot = rootLabel !== null
  const keys: Key[] = [...(withRoot ? [TOP as Key] : []), ...visible.map((row) => row.folder.id)]
  const selectedKey: Key = selected ?? TOP
  const [focused, setFocused] = useState<Key>(selectedKey)
  const tabStop = keys.includes(focused) ? focused : keys.includes(selectedKey) ? selectedKey : keys[0]
  const treeRef = useRef<HTMLUListElement>(null)

  function rowOf(key: Key): HTMLElement | null {
    return treeRef.current?.querySelector<HTMLElement>(`[data-key="${key}"]`) ?? null
  }

  function focusKey(key: Key) {
    setFocused(key)
    rowOf(key)?.focus()
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
    if ((event.key === 'ContextMenu' || (event.key === 'F10' && event.shiftKey)) && folder && wrapRow) {
      openMenuAt(rowOf(key)?.querySelector<HTMLElement>('[data-row]') ?? null)
      event.preventDefault()
      event.stopPropagation()
      return
    }
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
        else if (folder) {
          const parent = folder.parent_id != null && keys.includes(folder.parent_id) ? folder.parent_id : TOP
          if (keys.includes(parent)) focusKey(parent)
        }
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
    cn(
      'flex h-8 w-full min-w-0 items-center gap-1.5 rounded-md pr-2 text-left text-sm transition-colors',
      'group-focus-visible/item:ring-2 group-focus-visible/item:ring-ring',
      selectedKey === key
        ? 'bg-accent font-medium text-accent-foreground'
        : 'text-muted-foreground hover:bg-accent/60 hover:text-foreground',
    )

  function renderFolder(folder: LibraryFolderView, level: number) {
    const open = expanded.has(folder.id)
    const branch = hasChildren(folder.id)
    const isSelected = selectedKey === folder.id
    const countText = fileCountText(folder)
    const row = (
      <div
        data-row
        className={rowClass(folder.id)}
        style={{ paddingLeft: `${4 + (level - 1) * 14}px` }}
      >
        {branch ? (
          <span
            aria-hidden
            data-testid={`${testIdPrefix}-toggle-${folder.id}`}
            onClick={(event) => {
              event.stopPropagation()
              toggle(folder.id)
            }}
            className="grid size-5 shrink-0 cursor-pointer place-items-center rounded-sm text-muted-foreground hover:bg-muted hover:text-foreground"
          >
            <ChevronRightIcon className={cn('size-3.5 transition-transform', open && 'rotate-90')} />
          </span>
        ) : (
          <span aria-hidden className="size-5 shrink-0" />
        )}
        {isSelected ? (
          <FolderOpenIcon aria-hidden className="size-4 shrink-0" />
        ) : (
          <FolderIcon aria-hidden className="size-4 shrink-0 opacity-70" />
        )}
        <span className="truncate">{folder.name}</span>
        {folder.project_id != null && (
          <span
            className="shrink-0 text-[11px] text-faint"
            aria-hidden
            // The name is in the treeitem's for a screen reader; this is for the eye (#3113).
            title={`Project ${folder.project_name ?? folder.project_id}`}
          >
            project
          </span>
        )}
        {folder.file_count ? (
          <Badge aria-hidden variant="secondary" className="sb-num ml-auto h-4.5 px-1.5 text-[10px] tabular-nums">
            {folder.file_count}
          </Badge>
        ) : null}
        {countText && (
          <span id={`${countId}-${folder.id}`} className="sr-only">
            {countText}
          </span>
        )}
      </div>
    )
    return (
      <Collapsible key={folder.id} open={branch && open} asChild>
        <li
          data-key={folder.id}
          role="treeitem"
          aria-level={level}
          aria-selected={isSelected}
          aria-expanded={branch ? open : undefined}
          aria-label={folder.name}
          aria-describedby={countText ? `${countId}-${folder.id}` : undefined}
          tabIndex={tabStop === folder.id ? 0 : -1}
          data-testid={`${testIdPrefix}-${folder.id}`}
          onKeyDown={(event) => onKeyDown(event, folder.id)}
          onFocus={(event) => {
            if (event.target === event.currentTarget) setFocused(folder.id)
          }}
          onClick={(event) => {
            event.stopPropagation()
            setFocused(folder.id)
            onSelect(folder.id)
          }}
          className="group/item outline-none"
        >
          {wrapRow ? wrapRow(folder, row) : row}
          {branch && (
            <CollapsibleContent asChild>
              <ul role="group" className="mt-0.5 space-y-0.5">
                {(children.get(folder.id) ?? []).map((child) => renderFolder(child, level + 1))}
              </ul>
            </CollapsibleContent>
          )}
        </li>
      </Collapsible>
    )
  }

  return (
    <ul ref={treeRef} role="tree" aria-label={label} className="space-y-0.5">
      {withRoot && (
        <li
          data-key={TOP}
          role="treeitem"
          aria-level={1}
          aria-selected={selectedKey === TOP}
          tabIndex={tabStop === TOP ? 0 : -1}
          data-testid={`${testIdPrefix}-root`}
          onKeyDown={(event) => onKeyDown(event, TOP)}
          onFocus={(event) => {
            if (event.target === event.currentTarget) setFocused(TOP)
          }}
          onClick={() => {
            setFocused(TOP)
            onSelect(null)
          }}
          className="group/item outline-none"
        >
          <div className={rowClass(TOP)} style={{ paddingLeft: '4px' }}>
            <span aria-hidden className="size-5 shrink-0" />
            <LibraryIcon aria-hidden className="size-4 shrink-0 opacity-70" />
            {rootLabel}
          </div>
        </li>
      )}
      {(children.get(null) ?? []).map((folder) => renderFolder(folder, 1))}
    </ul>
  )
}
