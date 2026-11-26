import { Trash2Icon } from 'lucide-react'
import { KEY_TYPES, newRow, type KeyRow, type KeyType } from './propertyKeys'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import { Input } from '@/components/ui/input'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'

/**
 * A kind's property keys as a table: each key's name, type and description, and, for a kind
 * whose rows are checked (`requirable`, #7703), whether the key is required.
 */
export function PropertyKeyTable({ label, rows, onChange, requirable = false }: { label: string; rows: KeyRow[]; onChange: (rows: KeyRow[]) => void; requirable?: boolean }) {
  function edit(at: number, patch: Partial<KeyRow>) {
    onChange(rows.map((row, i) => (i === at ? { ...row, ...patch } : row)))
  }

  return (
    <>
      {rows.length > 0 && (
        <table className="w-full max-w-2xl text-left" aria-label={label}>
          <thead className="text-xs text-muted-foreground">
            <tr>
              <th className="py-1 pr-2 font-medium">Key</th>
              <th className="py-1 pr-2 font-medium">Type</th>
              <th className="py-1 pr-2 font-medium">Description</th>
              {requirable && <th className="py-1 pr-2 font-medium">Required</th>}
              <th className="py-1">
                <span className="sr-only">Remove</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row, at) => (
              <tr key={row.rid}>
                <td className="py-1 pr-2">
                  <Input aria-label={`Key ${at + 1}`} value={row.key} onChange={(e) => edit(at, { key: e.target.value })} className="h-8 w-36 font-mono" />
                </td>
                <td className="py-1 pr-2">
                  <Select value={row.type} onValueChange={(next) => edit(at, { type: next as KeyType })}>
                    <SelectTrigger size="sm" aria-label={`Type of key ${at + 1}`} className="w-36">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent position="popper">
                      {(Object.keys(KEY_TYPES) as KeyType[]).map((type) => (
                        <SelectItem key={type} value={type}>
                          {KEY_TYPES[type]}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </td>
                <td className="py-1 pr-2">
                  <Input aria-label={`Description of key ${at + 1}`} value={row.description} onChange={(e) => edit(at, { description: e.target.value })} className="h-8 w-full min-w-40" />
                </td>
                {requirable && (
                  <td className="py-1 pr-2 text-center">
                    <Checkbox aria-label={`Key ${at + 1} is required`} checked={row.required} onCheckedChange={(next) => edit(at, { required: next === true })} />
                  </td>
                )}
                <td className="py-1">
                  <Button type="button" size="icon-sm" variant="ghost" aria-label={`Remove key ${at + 1}`} onClick={() => onChange(rows.filter((_, i) => i !== at))}>
                    <Trash2Icon />
                  </Button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <div>
        <Button type="button" size="xs" variant="outline" onClick={() => onChange([...rows, newRow()])}>
          Add key
        </Button>
      </div>
    </>
  )
}
