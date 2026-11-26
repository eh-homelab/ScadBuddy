import { Badge } from '@/components/ui/badge'

/**
 * #7703 — a row its required properties leave incomplete: "Missing required: a, b", and the keys
 * whose stored value no longer fits. Nothing for a complete row (an archived one is never marked).
 */
export function IncompleteMark({ row }: { row: { missing_properties?: string[]; invalid_properties?: string[] } }) {
  const missing = row.missing_properties ?? []
  const invalid = row.invalid_properties ?? []
  if (missing.length === 0 && invalid.length === 0) return null
  return (
    <span className="inline-flex flex-wrap gap-1">
      {missing.length > 0 && (
        <Badge variant="outline" className="font-normal text-warn">
          Missing required: {missing.join(', ')}
        </Badge>
      )}
      {invalid.length > 0 && (
        <Badge variant="outline" className="font-normal text-warn">
          Does not fit: {invalid.join(', ')}
        </Badge>
      )}
    </span>
  )
}
