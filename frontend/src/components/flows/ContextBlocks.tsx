import { useState } from 'react'
import { Link } from 'react-router'
import { Markdown } from '../../agent/chat/Markdown'
import type { TaskQuestion } from '../../api/types'
import { expectedText } from '../../lib/flowInputs'
import { entityApi, entityParts } from '../entities/api'
import { Checkbox } from '@/components/ui/checkbox'
import { INSET } from './pendingParts'

const isObject = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null && !Array.isArray(value)
const isScalar = (value: unknown) => value === null || ['string', 'number', 'boolean'].includes(typeof value)

/** Where each kind of entity a flow may name has its page. */
const PAGES: Record<string, (ref: string) => string> = {
  order: (ref) => `/orders/${encodeURIComponent(ref)}`,
  customer: (ref) => `/customers/${encodeURIComponent(ref)}`,
  spool: (ref) => `/filament/spools/${encodeURIComponent(ref)}`,
  location: (ref) => `/filament/locations/${encodeURIComponent(ref)}`,
  spool_type: (ref) => `/filament/types/${encodeURIComponent(ref)}`,
  supplier: (ref) => `/filament/suppliers/${encodeURIComponent(ref)}`,
  invoice: (ref) => `/filament/invoices/${encodeURIComponent(ref)}`,
  model: (ref) => `/m/${encodeURIComponent(ref)}`,
}

/** A value as text: a list of plain values as they read, anything deeper as JSON. */
function contextText(value: unknown): string {
  if (typeof value === 'string') return value
  if (value === null) return '—'
  const plain = Array.isArray(value) && value.length > 0 && value.every((item) => typeof item !== 'object')
  return plain ? value.join(', ') : JSON.stringify(value)
}

function Checklist({ items }: { items: string[] }) {
  // Ticks are for the person at the screen: nothing is sent or kept.
  const [ticked, setTicked] = useState<ReadonlySet<number>>(new Set())
  return (
    <ul className="flex flex-col gap-1">
      {items.map((item, i) => (
        <li key={i}>
          <label className="flex min-h-9 items-start gap-2 py-1 text-[13px]">
            <Checkbox
              className="mt-0.5 size-5 shrink-0"
              aria-label={item}
              checked={ticked.has(i)}
              onCheckedChange={(on) =>
                setTicked((now) => {
                  const next = new Set(now)
                  if (on === true) next.add(i)
                  else next.delete(i)
                  return next
                })
              }
            />
            <span className="min-w-0 break-words">{item}</span>
          </label>
        </li>
      ))}
    </ul>
  )
}

/** The block a one-key item draws, or null when its key or its shape is not a block's. */
function block(item: Record<string, unknown>) {
  const keys = Object.keys(item)
  if (keys.length !== 1) return null
  const key = keys[0]!
  const value = item[key]
  if (key === 'text' && typeof value === 'string') return <Markdown text={value} />
  if (key === 'entity' && typeof value === 'string') {
    const { kind, ref } = entityParts(value)
    const page = PAGES[kind]
    const label = `${kind.replace(/_/g, ' ')} ${ref}`
    return page && ref ? (
      <Link to={page(ref)} className="text-[13px] underline break-words">
        {label}
      </Link>
    ) : (
      <span className="text-[13px] break-words">{value}</span>
    )
  }
  if (key === 'image' && isObject(value) && typeof value['entity'] === 'string' && typeof value['attachment_id'] === 'string') {
    return (
      <img
        src={entityApi.attachmentUrl(value['entity'], value['attachment_id'])}
        alt="Attached to this question"
        className="max-h-64 max-w-full rounded-md object-contain"
      />
    )
  }
  if (key === 'table' && isObject(value)) {
    const { columns, rows } = value
    const ok =
      Array.isArray(columns) &&
      columns.every((column) => typeof column === 'string') &&
      Array.isArray(rows) &&
      rows.every((row) => Array.isArray(row) && row.every(isScalar))
    if (!ok) return null
    return (
      <div className="overflow-x-auto">
        <table className="w-full text-left text-[12.5px]">
          <thead>
            <tr>
              {(columns as string[]).map((column, i) => (
                <th key={i} scope="col" className="border-b px-2 py-1 font-medium">
                  {column}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {(rows as unknown[][]).map((row, r) => (
              <tr key={r} className="not-last:border-b">
                {row.map((cell, c) => (
                  <td key={c} className="px-2 py-1 break-words">
                    {cell === null ? '—' : String(cell)}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    )
  }
  if (key === 'checklist' && Array.isArray(value) && value.every((entry) => typeof entry === 'string')) {
    return <Checklist items={value as string[]} />
  }
  if (key === 'expect' && isObject(value) && typeof value['weight_g'] === 'number') {
    const tolerance = value['tolerance_g']
    return (
      <p className="text-base font-medium">
        {expectedText({ weight_g: value['weight_g'], ...(typeof tolerance === 'number' ? { tolerance_g: tolerance } : {}) })}
      </p>
    )
  }
  return null
}

/**
 * #8062 (R8) — what the flow gave with a task. An item with one key of `text`, `entity`,
 * `image`, `table`, `checklist` or `expect` is drawn as that block; any other dict is a
 * definition list, as A1 drew every item. All of it is text and elements, never markup.
 */
export function ContextBlocks({ items }: { items: TaskQuestion['context'] }) {
  const shown = items.filter((item) => Object.keys(item).length > 0)
  if (shown.length === 0) return null
  return (
    <div
      role="group"
      aria-label="Context"
      // It scrolls, so the keyboard must reach it.
      tabIndex={0}
      className={`mb-3 flex max-h-64 flex-col gap-2 overflow-y-auto outline-none focus-visible:ring-3 focus-visible:ring-ring/50 ${INSET}`}
    >
      {shown.map((item, i) => (
        <div key={i} className="not-first:border-t not-first:pt-2">
          {block(item) ?? (
            <dl className="grid grid-cols-[fit-content(40%)_minmax(0,1fr)] gap-x-3 gap-y-0.5 text-[12.5px]">
              {Object.entries(item).map(([name, value]) => (
                <div key={name} className="contents">
                  <dt className="min-w-0 wrap-anywhere text-muted-foreground">{name}</dt>
                  <dd className="min-w-0 wrap-anywhere whitespace-pre-wrap">{contextText(value)}</dd>
                </div>
              ))}
            </dl>
          )}
        </div>
      ))}
    </div>
  )
}
