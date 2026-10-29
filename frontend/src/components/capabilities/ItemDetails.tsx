import { useState } from 'react'
import { aiCapabilities, type ItemDetails as Details, type ItemView, type Omitted } from '../../api/capabilities'
import { copyText } from '../../lib/clipboard'
import { Button } from '@/components/ui/button'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'
import { Spinner } from '@/components/ui/spinner'
import { parametersOf, type Param } from './schema'

const OMITTED: Record<Omitted, string> = {
  title: 'Title',
  description: 'Description',
  inputSchema: 'Input schema',
  outputSchema: 'Output schema',
  annotations: 'Annotations',
  keys: 'Some keys',
  other: 'Other fields',
}

const message = (error: unknown) => (error instanceof Error ? error.message : String(error))

/**
 * An item's full entry (#2171), read on first open: everything in it but `item` is untrusted
 * server text, so every value is a text node; nothing is parsed as HTML or markdown.
 */
export function ItemDetails({
  source,
  item,
  claims,
}: {
  source: string
  item: ItemView
  /** Label the annotations as the server's claims (a source that is not built in). */
  claims: boolean
}) {
  const [details, setDetails] = useState<Details | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function load() {
    setLoading(true)
    setError(null)
    try {
      setDetails(await aiCapabilities.details(source, item.id))
    } catch (caught) {
      setError(message(caught))
    } finally {
      setLoading(false)
    }
  }

  return (
    <Collapsible onOpenChange={(open) => open && details === null && !loading && void load()}>
      <CollapsibleTrigger asChild>
        <Button size="xs" variant="link" className="px-0" aria-label={`Details of ${item.name}`}>
          Details
        </Button>
      </CollapsibleTrigger>
      <CollapsibleContent className="flex flex-col gap-2 text-xs">
        {loading && <Spinner />}
        {error !== null && (
          <p role="alert" className="text-warn">
            {error}
          </p>
        )}
        {details !== null && <Body details={details} name={item.name} claims={claims} />}
      </CollapsibleContent>
    </Collapsible>
  )
}

function Body({ details, name, claims }: { details: Details; name: string; claims: boolean }) {
  const [json, setJson] = useState(false)
  const raw = JSON.stringify(details.raw, null, 2)
  const title = typeof details.raw?.title === 'string' ? details.raw.title : details.item.label
  const output = parametersOf(details.output_schema)
  const annotations = Object.entries(details.annotations ?? {})
  return (
    <>
      <div className="font-medium">{title}</div>
      {details.item.description !== null && <p className="whitespace-pre-wrap">{details.item.description}</p>}
      {details.omitted.map((part) => (
        <p key={part} className="text-muted-foreground">{`${OMITTED[part]}: too large to keep`}</p>
      ))}
      <Parameters caption={`Parameters of ${name}`} params={parametersOf(details.input_schema)} />
      {output.length > 0 && <Parameters caption={`Output of ${name}`} params={output} />}
      {annotations.length > 0 && (
        <div>
          {claims && <p className="text-muted-foreground">Claims made by the server, not checked by ScadBuddy.</p>}
          <ul className="sb-num">
            {annotations.map(([key, value]) => (
              <li key={key}>{`${key}: ${typeof value === 'string' ? value : JSON.stringify(value)}`}</li>
            ))}
          </ul>
        </div>
      )}
      {details.raw !== null && (
        <div className="flex flex-col gap-1">
          <div className="flex gap-2">
            <Button size="xs" variant="outline" aria-expanded={json} onClick={() => setJson(!json)}>
              View JSON
            </Button>
            {json && (
              <Button size="xs" variant="outline" onClick={() => void copyText(raw)}>
                Copy
              </Button>
            )}
          </div>
          {json && (
            <pre data-testid="raw-json" className="sb-num max-h-80 overflow-auto rounded-md bg-muted p-2">
              {raw}
            </pre>
          )}
        </div>
      )}
    </>
  )
}

function Parameters({ caption, params }: { caption: string; params: Param[] }) {
  if (params.length === 0) return <p className="text-muted-foreground">No parameters</p>
  return (
    <table aria-label={caption} className="w-full text-left">
      <thead>
        <tr>
          <th className="pr-2 font-medium">Name</th>
          <th className="pr-2 font-medium">Type</th>
          <th className="font-medium">About</th>
        </tr>
      </thead>
      <tbody>
        {params.map((p) => (
          <tr key={p.name} className="align-top">
            <td className="sb-num pr-2">
              {p.name}
              {p.required && <span className="text-muted-foreground"> required</span>}
            </td>
            <td className="sb-num pr-2">{p.type}</td>
            <td>
              {p.description !== null && <span className="whitespace-pre-wrap">{p.description}</span>}
              {p.enum !== null && <span className="text-muted-foreground">{` one of ${p.enum.join(', ')}`}</span>}
              {p.default !== null && <span className="text-muted-foreground">{` default ${p.default}`}</span>}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}
