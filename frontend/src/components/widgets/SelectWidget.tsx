import { useRef } from 'react'
import type { Param, ParamValue } from '../../api/types'
import { useFillable } from '../../agent/useFillable'
import { Field } from './Field'
import { paramLabel } from '../../lib/params'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'

/** Radix refuses an item whose value is "", which an OpenSCAD option may well be. */
const EMPTY = '\u0000empty'
const keyOf = (value: ParamValue) => (String(value) === '' ? EMPTY : String(value))

export function SelectWidget({
  param,
  value,
  onChange,
}: {
  param: Param
  value: ParamValue
  onChange: (next: ParamValue) => void
}) {
  const id = `p-${param.name}`
  const options = param.options ?? []
  const trigger = useRef<HTMLButtonElement>(null)
  // The assistant's fill sets it by value or label, as it did the native select (#3672).
  useFillable(trigger, {
    // The raw values, "" included: the sentinel is only for Radix, which refuses "".
    options: () => options.map((option) => ({ value: String(option.value), label: option.name })),
    value: () => String(value),
    set: (next) => {
      // `fill` sets only a value `options` lists, so this always finds one.
      const picked = options.find((option) => String(option.value) === next)
      if (picked) onChange(picked.value)
    },
  })

  return (
    <Field id={id} label={paramLabel(param)} name={param.name}>
      <Select
        value={keyOf(value)}
        onValueChange={(next) => {
          const picked = options.find((option) => keyOf(option.value) === next)
          onChange(picked ? picked.value : next === EMPTY ? '' : next)
        }}
      >
        <SelectTrigger ref={trigger} id={id} className="w-full">
          <SelectValue />
        </SelectTrigger>
        <SelectContent position="popper">
          {options.map((option) => (
            <SelectItem key={keyOf(option.value)} value={keyOf(option.value)}>
              {option.name}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    </Field>
  )
}
