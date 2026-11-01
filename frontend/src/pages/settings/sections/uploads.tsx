import { api } from '../../../api/client'
import { Card, CardContent } from '@/components/ui/card'
import { Textarea } from '@/components/ui/textarea'
import { formatBytes } from '../../../lib/format'
import { useAsync } from '../../../lib/useAsync'
import { FieldRow } from '../controls'
import { RuntimeRows, SavedSection } from '../formParts'
import type { AreaId } from '../registry'
import { useBackendSettings } from '../useBackendSettings'
import { FETCH_NOWHERE, fieldsOf, ofLimit } from '../values'

export const id = 'uploads'
export const area: AreaId = 'library'
export const title = 'Uploads'
export const order = 20

/** #296 — the upload caps, and how much the upload store holds against them. */
export function Component() {
  const form = useBackendSettings(id, (s) => fieldsOf(id, s))
  const usage = useAsync(() => api.getAssetUsage(), []).data
  const { value, setField, errors } = form
  return (
    <SavedSection form={form} id={id} title={title}>
      <FieldRow
        id="asset-fetch-domains"
        label="Fetch assets from"
        help={`The domains an SVG or PNG may be fetched from by URL (the assistant's fetch_asset), a domain a line; each takes its subdomains, and every redirect must stay on the list. Empty restores the defaults; "${FETCH_NOWHERE}" allows none.`}
        error={errors.asset_fetch_domains}
      >
        <Textarea
          id="asset-fetch-domains"
          rows={4}
          spellCheck={false}
          value={value('asset_fetch_domains')}
          onChange={(event) => setField('asset_fetch_domains', event.target.value)}
          className="font-mono"
        />
      </FieldRow>
      <RuntimeRows form={form} section={id} />
      {usage && (
        <Card size="sm">
          <CardContent>
          <p className="text-sm font-medium">Uploaded files</p>
          <dl className="mt-2 grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-sm" data-testid="asset-usage">
            <dt className="text-muted-foreground">Files</dt>
            <dd className="sb-num">{ofLimit(String(usage.count), usage.max_count, String)}</dd>
            <dt className="text-muted-foreground">Size</dt>
            <dd className="sb-num">{ofLimit(formatBytes(usage.bytes), usage.max_total_bytes, formatBytes)}</dd>
          </dl>
          <p className="mt-2 text-xs text-muted-foreground">
            The SVGs and PNGs attached to file parameters. Past either limit, a new upload is refused.
          </p>
          </CardContent>
        </Card>
      )}
    </SavedSection>
  )
}
