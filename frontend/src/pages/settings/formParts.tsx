import { useState, type ReactNode } from 'react'
import { ChevronRightIcon } from 'lucide-react'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'
import { cn } from '@/lib/utils'
import { FieldRow, RuntimeInput, Section, SourceBadge } from './controls'
import { RUNTIME_FIELDS, type FieldName, type FieldSpec, type SectionId } from './fields'
import type { BackendSettings } from './useBackendSettings'
import { extraSpecs, isSecret, type Secret } from './values'

/** A section saved through `form`: its Save and Discard, and its save and reset errors. */
export function SavedSection({
  form,
  id,
  title,
  description,
  children,
}: {
  form: BackendSettings
  id: string
  title: string
  description?: ReactNode
  children: ReactNode
}) {
  return (
    <Section
      id={id}
      title={title}
      description={description}
      dirty={form.dirty}
      saving={form.saving}
      savedAt={form.savedAt}
      onSave={() => void form.save()}
      onDiscard={form.discard}
    >
      {children}
      {form.sectionError && (
        <p role="alert" className="text-sm text-warn">
          {form.sectionError}
        </p>
      )}
      {form.resetError && (
        <p role="alert" className="text-sm text-warn">
          {form.resetError}
        </p>
      )}
    </Section>
  )
}

/** Where `name`'s value came from, with its Reset. */
export function FieldBadge({ form, name }: { form: BackendSettings; name: FieldName }) {
  return (
    <SourceBadge
      name={name}
      settings={form.settings}
      onReset={() => void form.reset(name)}
      resetting={form.resetting === name}
    />
  )
}

function RuntimeRow({ form, spec }: { form: BackendSettings; spec: FieldSpec }) {
  return (
    <FieldRow
      id={`setting-${spec.name}`}
      label={spec.label}
      badge={<FieldBadge form={form} name={spec.name} />}
      help={spec.help}
      error={form.errors[spec.name]}
    >
      <RuntimeInput
        spec={spec}
        value={form.value(spec.name)}
        onChange={(next) => form.setField(spec.name, next)}
        settings={form.settings}
        clearing={isSecret(spec.name) && form.clearing.includes(spec.name)}
        onClear={isSecret(spec.name) ? () => form.clearSecret(spec.name as Secret) : undefined}
      />
    </FieldRow>
  )
}

/** The runtime fields of `section`, the advanced ones in their own disclosure. */
export function RuntimeRows({ form, section }: { form: BackendSettings; section: SectionId }) {
  const specs = [...RUNTIME_FIELDS, ...extraSpecs(form.settings)].filter((spec) => spec.section === section)
  const advanced = specs.filter((spec) => spec.advanced)
  return (
    <>
      {specs
        .filter((spec) => !spec.advanced)
        .map((spec) => (
          <RuntimeRow key={spec.name} form={form} spec={spec} />
        ))}
      {advanced.length > 0 && (
        <Advanced label="Advanced" invalid={advanced.some((spec) => Boolean(form.errors[spec.name]))}>
          {advanced.map((spec) => (
            <RuntimeRow key={spec.name} form={form} spec={spec} />
          ))}
        </Advanced>
      )}
    </>
  )
}

export function Advanced({ label, invalid, children }: { label: string; invalid: boolean; children: ReactNode }) {
  const [open, setOpen] = useState(false)
  const [wasInvalid, setWasInvalid] = useState(invalid)
  if (invalid !== wasInvalid) {
    setWasInvalid(invalid)
    if (invalid) setOpen(true)
  }
  return (
    <Collapsible open={open} onOpenChange={setOpen} className="rounded-lg ring-1 ring-foreground/10">
      <CollapsibleTrigger className="flex min-h-9 w-full items-center gap-1.5 rounded-lg px-3 py-2 text-left text-sm text-muted-foreground hover:bg-muted/50 hover:text-foreground">
        <ChevronRightIcon aria-hidden="true" className={cn('size-4 shrink-0 transition-transform', open && 'rotate-90')} />
        {label}
      </CollapsibleTrigger>
      <CollapsibleContent forceMount hidden={!open} className="space-y-4 border-t p-3">
        {children}
      </CollapsibleContent>
    </Collapsible>
  )
}
