import { useEffect, useLayoutEffect, useRef, type RefObject } from 'react'
import { registerFillable, type Fillable } from './fillable'

/**
 * Registers `ref`'s element for `fill` while it is mounted; the latest props are read on
 * each call. Checked after every commit, since the element can mount after the component
 * does (a dialog's content, a list that loads) or be replaced by another one.
 */
export function useFillable(ref: RefObject<Element | null>, fillable: Fillable): void {
  const latest = useRef(fillable)
  useLayoutEffect(() => {
    latest.current = fillable
  })
  const registered = useRef<{ element: Element; release: () => void } | null>(null)
  // No deps, deliberately: a ref's element can change without the ref doing so. Cheap,
  // since it returns at once while the element is the one registered.
  useEffect(() => {
    const element = ref.current
    if (registered.current?.element === element) return
    registered.current?.release()
    registered.current = element
      ? {
          element,
          release: registerFillable(element, {
            options: () => latest.current.options(),
            value: () => latest.current.value(),
            set: (value) => latest.current.set(value),
          }),
        }
      : null
  })
  useEffect(
    () => () => {
      registered.current?.release()
      registered.current = null
    },
    [],
  )
}
