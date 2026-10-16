import { act, screen, waitFor } from '@testing-library/react'
import { useRef, useState } from 'react'
import { describe, expect, it } from 'vitest'
import {
  AlertDialog,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogCancel,
} from '@/components/ui/alert-dialog'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { renderPage } from '../test/utils'
import { bridge } from './bridge'
import { useGlobalAgentTools } from './global'
import { useFillable } from './useFillable'

const OPTIONS = [
  { value: 'mm', label: 'Millimetres' },
  { value: 'in', label: 'Inches' },
]

/** A Radix select that is not there on the first render, as a loading page's or a closed dialog's is. */
function Late({ asks = false }: { asks?: boolean }) {
  useGlobalAgentTools()
  const [loaded, setLoaded] = useState(false)
  const [unit, setUnit] = useState('mm')
  const [pending, setPending] = useState<string | null>(null)
  const trigger = useRef<HTMLButtonElement>(null)
  useFillable(trigger, {
    options: () => OPTIONS,
    value: () => unit,
    set: (next) => (asks ? setPending(next) : setUnit(next)),
  })
  if (!loaded) {
    return (
      <button type="button" onClick={() => setLoaded(true)}>
        Load
      </button>
    )
  }
  return (
    <>
      <label htmlFor="unit">Unit</label>
      <Select value={unit} onValueChange={setUnit}>
        <SelectTrigger ref={trigger} id="unit">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          {OPTIONS.map((option) => (
            <SelectItem key={option.value} value={option.value}>
              {option.label}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
      <p data-testid="unit">{unit}</p>
      <AlertDialog open={pending !== null} onOpenChange={(open) => !open && setPending(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Change the unit?</AlertDialogTitle>
            <AlertDialogDescription>Your values stay as typed.</AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  )
}

describe('useFillable', () => {
  it('registers a select that mounts after its component (#3672)', async () => {
    renderPage(<Late />)
    await waitFor(() => expect(bridge.liveNames()).toContain('fill'))
    act(() => screen.getByRole('button', { name: 'Load' }).click())
    await screen.findByRole('combobox', { name: 'Unit' })

    expect(await bridge.call('fill', { label: 'Unit', value: 'Inches' })).toEqual({
      ok: true,
      result: { filled: 'Unit', value: 'in' },
    })
    expect(screen.getByTestId('unit')).toHaveTextContent('in')
  })

  it('says a set that opened a confirmation is waiting on it', async () => {
    renderPage(<Late asks />)
    await waitFor(() => expect(bridge.liveNames()).toContain('fill'))
    act(() => screen.getByRole('button', { name: 'Load' }).click())
    await screen.findByRole('combobox', { name: 'Unit' })

    const outcome = await bridge.call('fill', { label: 'Unit', value: 'in' })
    expect(outcome.ok && outcome.result).toMatchObject({ filled: 'Unit', value: 'mm', confirm: expect.any(String) })
    expect(screen.getByRole('alertdialog', { name: 'Change the unit?' })).toBeInTheDocument()
    expect(screen.getByTestId('unit')).toHaveTextContent('mm')
  })
})
