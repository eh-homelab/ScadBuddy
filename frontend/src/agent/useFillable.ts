import { useEffect, useLayoutEffect, useRef, type RefObject } from 'react'
import { registerFillable, type Fillable } from './fillable'

/** Registers `ref`'s element for `fill` while it is mounted; the latest props are read on each call. */
export function useFillable(ref: RefObject<Element | null>, fillable: Fillable): void {
  const latest = useRef(fillable)
  useLayoutEffect(() => {
    latest.current = fillable
  })
  useEffect(() => {
    const element = ref.current
    if (!element) return
    return registerFillable(element, {
      options: () => latest.current.options(),
      value: () => latest.current.value(),
      set: (value) => latest.current.set(value),
    })
  }, [ref])
}
