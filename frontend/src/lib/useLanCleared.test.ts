import { act, renderHook } from '@testing-library/react'
import { useState } from 'react'
import { describe, expect, it } from 'vitest'
import type { PrintOptions } from '../api/types'
import { useLanCleared } from './useLanCleared'

/** The hook over a dialog's own options, as `PrintPicker` holds them. */
function useDialog(props: { native: boolean; printer: string; seed: PrintOptions }) {
  const [options, setOptions] = useState<PrintOptions>(props.seed)
  const lan = useLanCleared(props.native, props.printer, options, setOptions)
  return { options, setOptions, ...lan }
}

const FLOW_ON = 'Flow calibration On was cleared: the LAN cannot set it.'

describe('useLanCleared (#5602)', () => {
  it('drops what the LAN cannot set, names it, and puts it back on another printer', () => {
    const { result, rerender } = renderHook(useDialog, {
      initialProps: { native: false, printer: 'bambuddy', seed: { flow_cali: 'on', timelapse: true } },
    })
    expect(result.current.notice).toBeNull()

    rerender({ native: true, printer: 'lan', seed: {} })
    expect(result.current.options).toEqual({ timelapse: true })
    expect(result.current.notice).toBe(FLOW_ON)

    rerender({ native: false, printer: 'bambuddy', seed: {} })
    expect(result.current.options).toEqual({ flow_cali: 'on', timelapse: true })
    expect(result.current.notice).toBeNull()
  })

  it('puts nothing back while the same printer reads as not native for a moment', () => {
    const { result, rerender } = renderHook(useDialog, {
      initialProps: { native: true, printer: 'lan', seed: { flow_cali: 'on' } },
    })
    expect(result.current.options).toEqual({})

    // Its route is read again: not native for a moment, the same printer chosen.
    rerender({ native: false, printer: 'lan', seed: {} })
    expect(result.current.options).toEqual({})
    rerender({ native: true, printer: 'lan', seed: {} })
    expect(result.current.notice).toBe(FLOW_ON)

    // Another LAN printer, then a Bambuddy one: it comes back once.
    rerender({ native: true, printer: 'lan-2', seed: {} })
    rerender({ native: false, printer: 'lan-2', seed: {} })
    expect(result.current.options).toEqual({})
    rerender({ native: false, printer: 'bambuddy', seed: {} })
    expect(result.current.options).toEqual({ flow_cali: 'on' })
  })

  it("keeps the user's later choice over a cleared value, and clear forgets them", () => {
    const { result, rerender } = renderHook(useDialog, {
      initialProps: { native: true, printer: 'lan', seed: { flow_cali: 'on', vibration_cali: true } },
    })
    act(() => result.current.setOptions({ flow_cali: 'off' }))
    rerender({ native: false, printer: 'bambuddy', seed: {} })
    expect(result.current.options).toEqual({ flow_cali: 'off', vibration_cali: true })

    rerender({ native: true, printer: 'lan', seed: {} })
    expect(result.current.notice).toBe('Vibration calibration On was cleared: the LAN cannot set it.')
    act(() => result.current.clear())
    rerender({ native: false, printer: 'bambuddy', seed: {} })
    expect(result.current.options).toEqual({ flow_cali: 'off' })
  })
})
