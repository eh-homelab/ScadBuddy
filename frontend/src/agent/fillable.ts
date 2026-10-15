import { useEffect, useLayoutEffect, useRef, type RefObject } from 'react'

/**
 * A control that is not a native field, which the bridge's `fill` can still set (#3672):
 * a Radix select is a button and a listbox in a portal, with no value to write to. What
 * renders one registers its trigger here with its options, its value and how to set it,
 * and `fill` sets it as it would a `<select>`.
 */
export interface Fillable {
  options: () => { value: string; label: string }[]
  value: () => string
  set: (value: string) => void
}

const fillables = new WeakMap<Element, Fillable>()

export function fillableOf(element: Element): Fillable | undefined {
  return fillables.get(element)
}

/** Registers `ref`'s element for `fill` while it is mounted; the latest props are read on each call. */
export function useFillable(ref: RefObject<Element | null>, fillable: Fillable): void {
  const latest = useRef(fillable)
  useLayoutEffect(() => {
    latest.current = fillable
  })
  useEffect(() => {
    const element = ref.current
    if (!element) return
    fillables.set(element, {
      options: () => latest.current.options(),
      value: () => latest.current.value(),
      set: (value) => latest.current.set(value),
    })
    return () => {
      fillables.delete(element)
    }
  }, [ref])
}
