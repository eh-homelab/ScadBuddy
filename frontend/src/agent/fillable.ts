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

/**
 * Without React: the bridge's tests load this module (through `global.ts` and
 * `snapshot.ts`) in the agent package, which has no React. The hook is `useFillable.ts`.
 */
export function registerFillable(element: Element, fillable: Fillable): () => void {
  fillables.set(element, fillable)
  return () => {
    fillables.delete(element)
  }
}

export function fillableOf(element: Element): Fillable | undefined {
  return fillables.get(element)
}
