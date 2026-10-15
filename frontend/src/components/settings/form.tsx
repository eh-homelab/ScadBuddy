import { useId, useState, type ComponentProps, type ReactElement, type ReactNode } from 'react'
import { ChevronsUpDownIcon } from 'lucide-react'
import { USER_ONLY } from '../../agent/dom'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardFooter, CardHeader } from '@/components/ui/card'
import { Command, CommandItem, CommandList } from '@/components/ui/command'
import { Input } from '@/components/ui/input'
import { InputGroup, InputGroupAddon, InputGroupButton, InputGroupInput } from '@/components/ui/input-group'
import { Label } from '@/components/ui/label'
import { Popover, PopoverAnchor, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import { Skeleton } from '@/components/ui/skeleton'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Switch } from '@/components/ui/switch'
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '@/components/ui/tooltip'
import { cn } from '@/lib/utils'
import { Spinner } from '../ui/Spinner'

/**
 * The Settings page's building blocks on the shadcn components (#3671): a card per
 * setting, a labelled input, a switch with its label, a select that takes an empty value,
 * a Save footer and a tooltip that brings its own provider.
 */

/** One setting that saves on its own: a card with its heading, a body and, for a form, a footer. */
export function SettingCard({
  title,
  description,
  children,
  className,
  headingId,
  ...props
}: {
  title: ReactNode
  description?: ReactNode
  children: ReactNode
  className?: string
  /** The heading's id, for a caller that labels something else by it. */
  headingId?: string
} & Omit<ComponentProps<'div'>, 'title'>) {
  const ownId = useId()
  const id = headingId ?? ownId
  return (
    <section aria-labelledby={id} className={cn('mt-4', className)}>
      <Card className="gap-0 py-0" {...props}>
        <CardHeader className="border-b py-3">
          <h2 id={id} className="font-heading text-sm leading-snug font-medium">
            {title}
          </h2>
          {description && <CardDescription className="text-xs">{description}</CardDescription>}
        </CardHeader>
        {children}
      </Card>
    </section>
  )
}

/** A card's body: `space-y-4`, padded like the header. */
export function SettingBody({ className, ...props }: ComponentProps<'div'>) {
  return <CardContent className={cn('space-y-4 py-4', className)} {...props} />
}

/** The Save row at a card's foot: the submit button, what the last save said, and its error. */
export function SaveFooter({
  saving,
  disabled,
  saved,
  error,
  label = 'Save',
  children,
}: {
  saving: boolean
  disabled: boolean
  /** Shown after a save, while nothing has changed since. */
  saved?: ReactNode
  error?: string | null
  label?: string
  /** More buttons, after Save. */
  children?: ReactNode
}) {
  return (
    <CardFooter className="flex-wrap gap-3 py-3">
      <Button type="submit" size="sm" disabled={disabled || saving} aria-busy={saving} {...USER_ONLY}>
        {saving && <Spinner />}
        {label}
      </Button>
      {children}
      {saved && (
        <span role="status" className="text-xs text-ok">
          {saved}
        </span>
      )}
      {error && (
        <p role="alert" className="basis-full text-xs text-warn">
          {error}
        </p>
      )}
    </CardFooter>
  )
}

/** A label over its input; the id is made when the caller gives none. */
export function LabeledInput({
  label,
  className,
  ...props
}: { label: ReactNode } & ComponentProps<typeof Input>) {
  const ownId = useId()
  const id = props.id ?? ownId
  return (
    <div className={cn('grid gap-1.5', className)}>
      <Label htmlFor={id} className="font-normal">
        {label}
      </Label>
      <Input {...props} id={id} />
    </div>
  )
}

/** A switch and its label, side by side. User-only by default: these switches grant access. */
export function SwitchField({
  id,
  label,
  checked,
  onCheckedChange,
  disabled,
  busy,
  describedBy,
  userOnly = true,
  className,
}: {
  id?: string
  label: ReactNode
  checked: boolean
  onCheckedChange: (checked: boolean) => void
  disabled?: boolean
  busy?: boolean
  describedBy?: string
  userOnly?: boolean
  className?: string
}) {
  const ownId = useId()
  const switchId = id ?? ownId
  return (
    <div className={cn('flex items-start gap-3', className)} {...(userOnly ? USER_ONLY : {})}>
      <Switch
        id={switchId}
        checked={checked}
        onCheckedChange={onCheckedChange}
        disabled={disabled}
        aria-busy={busy}
        aria-describedby={describedBy}
        className="mt-0.5"
      />
      <Label htmlFor={switchId} className="leading-snug font-normal">
        {label}
        {busy && <Spinner />}
      </Label>
    </div>
  )
}

/** Radix's Select reserves the empty value for "nothing chosen"; a choice of "" goes as this. */
const EMPTY = '\u0000empty'

export type Choice = { value: string; label: ReactNode; disabled?: boolean }

/**
 * A select over string choices, one of which may be "" (None, Library root, the default).
 * The trigger takes `id`, so a `<label htmlFor>` (FieldRow's) names it.
 */
export function ChoiceSelect({
  id,
  value,
  onValueChange,
  choices,
  disabled,
  className,
  placeholder,
  size,
  ...aria
}: {
  id?: string
  value: string
  onValueChange: (value: string) => void
  choices: readonly Choice[]
  disabled?: boolean
  className?: string
  placeholder?: string
  size?: 'sm' | 'default'
  'aria-label'?: string
  'aria-describedby'?: string
}) {
  const wrap = (raw: string) => (raw === '' ? EMPTY : raw)
  return (
    <Select value={wrap(value)} onValueChange={(next) => onValueChange(next === EMPTY ? '' : next)} disabled={disabled}>
      <SelectTrigger id={id} size={size} className={cn('w-full max-w-full', className)} {...aria}>
        <SelectValue placeholder={placeholder} />
      </SelectTrigger>
      <SelectContent position="popper">
        {choices.map((choice) => (
          <SelectItem key={choice.value} value={wrap(choice.value)} disabled={choice.disabled}>
            {choice.label}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  )
}

/**
 * A tooltip on a focusable trigger. Its own provider, so a setting rendered outside the
 * app shell (a test, a story) still has one.
 */
export function Tip({ content, children }: { content: ReactNode; children: ReactElement }) {
  return (
    <TooltipProvider>
      <Tooltip>
        <TooltipTrigger asChild>{children}</TooltipTrigger>
        <TooltipContent>{content}</TooltipContent>
      </Tooltip>
    </TooltipProvider>
  )
}

export type Suggestion = { value: string; label?: ReactNode }

/**
 * A free-text field with a list of suggestions beside it (a combobox): what a
 * `<datalist>` gave, as a popover the keyboard reaches. The text stays the value; picking
 * a suggestion writes it into the field.
 */
export function SuggestInput({
  suggestions,
  onPick,
  suggestionsLabel,
  onOpen,
  className,
  ...props
}: {
  suggestions: readonly Suggestion[]
  /** A suggestion was picked: its value, for the caller to set. */
  onPick: (value: string) => void
  /** The suggestions button's name, e.g. "Suggested models". */
  suggestionsLabel: string
  /** The suggestions were opened: a caller that reads them lazily reads them now. */
  onOpen?: () => void
  className?: string
} & ComponentProps<'input'>) {
  const [open, setOpen] = useState(false)
  return (
    <Popover
      open={open}
      onOpenChange={(next) => {
        setOpen(next)
        if (next) onOpen?.()
      }}
    >
      <PopoverAnchor asChild>
        <InputGroup className={className}>
          {/* A combobox: typed into, with a list of suggestions it opens (WAI-ARIA APG). */}
          <InputGroupInput role="combobox" aria-expanded={open} aria-autocomplete="none" {...props} />
          <InputGroupAddon align="inline-end">
            <PopoverTrigger asChild>
              <InputGroupButton size="icon-xs" aria-label={suggestionsLabel} disabled={props.disabled}>
                <ChevronsUpDownIcon />
              </InputGroupButton>
            </PopoverTrigger>
          </InputGroupAddon>
        </InputGroup>
      </PopoverAnchor>
      <PopoverContent align="start" className="w-(--radix-popover-trigger-width) min-w-56 p-0">
        <Command label={suggestionsLabel}>
          <CommandList>
            {suggestions.map((suggestion) => (
              <CommandItem
                key={suggestion.value}
                value={suggestion.value}
                onSelect={() => {
                  onPick(suggestion.value)
                  setOpen(false)
                }}
              >
                <span className="sb-num">{suggestion.value}</span>
                {suggestion.label && <span className="text-xs text-muted-foreground">{suggestion.label}</span>}
              </CommandItem>
            ))}
          </CommandList>
        </Command>
      </PopoverContent>
    </Popover>
  )
}

/**
 * What is being read, as skeleton lines: the label is said (and kept for tests and the
 * browser agent) though not shown.
 */
export function LoadingLines({
  label,
  lines = ['w-2/3', 'w-1/2'],
  className,
}: {
  label: string
  /** Each line's width class. */
  lines?: readonly string[]
  className?: string
}) {
  return (
    <div role="status" aria-busy="true" className={cn('space-y-2', className)}>
      <span className="sr-only">{label}</span>
      {lines.map((width, index) => (
        <Skeleton key={index} className={cn('h-4', width)} />
      ))}
    </div>
  )
}
