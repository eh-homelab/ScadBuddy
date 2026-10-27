import { Badge } from '@/components/ui/badge'
import { Card } from '@/components/ui/card'
import { useBambuddyResources } from '../bambuddyResources'
import { useLoadedSettings } from '../cache'
import { Section } from '../controls'
import type { AreaId } from '../registry'

export const id = 'about'
export const area: AreaId = 'system'
export const title = 'About'
export const order = 30

/** The build, the versions it runs with, and what only the deployment sets. */
export function Component() {
  const settings = useLoadedSettings()
  const { connected, status } = useBambuddyResources()
  const bambuddy = status.data
  return (
    <Section id={id} title={title}>
      <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-4 gap-y-1 text-sm" data-testid="about">
        <dt className="text-muted-foreground">ScadBuddy</dt>
        <dd className="sb-num">
          {settings.about?.version ?? 'unknown'} ({settings.about?.revision ?? 'unknown'})
        </dd>
        <dt className="text-muted-foreground">OpenSCAD</dt>
        <dd className="sb-num">{settings.about?.openscad_version ?? 'not found'}</dd>
        <dt className="text-muted-foreground">Bambuddy</dt>
        <dd className="sb-num">{connected ? (bambuddy?.version ?? 'unknown') : 'not connected'}</dd>
      </dl>
      <div>
        <p className="text-sm font-medium">Set by the deployment only</p>
        <p className="mt-0.5 text-xs text-muted-foreground">
          These cannot be changed here. Each is its <span className="sb-num">SCADBUDDY_*</span> variable or
          the image&rsquo;s default.
        </p>
        {/* #1035 — `overflow-wrap: anywhere` on the variable names and values: they are
            single unbroken tokens (a comma-joined origin list runs ~570 px), and without
            a break point the table ran past the card at every width. */}
        <Card size="sm" className="mt-2 py-0">
          <table className="w-full text-left text-xs" aria-label="Deployment values">
            <tbody className="divide-y">
              {(settings.bootstrap ?? []).map((entry) => (
                <tr key={entry.name} className="align-top transition-colors hover:bg-muted/50">
                  <th scope="row" className="sb-num w-2/5 px-3 py-2 font-medium [overflow-wrap:anywhere]">
                    {entry.env_var}
                  </th>
                  <td className="px-3 py-2">
                    <span className="sb-num [overflow-wrap:anywhere]">{entry.value ?? 'unset'}</span>{' '}
                    <Badge variant="outline" className="ml-1 text-muted-foreground">
                      {entry.source === 'env' ? 'set' : 'default'}
                    </Badge>
                    <p className="mt-1 text-muted-foreground">{entry.reason}</p>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </Card>
      </div>
    </Section>
  )
}
