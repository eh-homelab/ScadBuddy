import { act, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { JobPicture, PICTURE_MAX_TRIES, PICTURE_RETRY_MS } from './JobPicture'

afterEach(() => {
  vi.useRealTimers()
})

function image(): HTMLImageElement | null {
  return screen.getByTestId('job-picture').querySelector('img')
}

const A = '/api/v1/farm/printers/1/cover?archive=90'
const B = '/api/v1/farm/printers/1/cover?archive=91'

/** Fails the shown image, then waits long enough for any next try. */
function failAndWait(): void {
  fireEvent.error(image()!)
  act(() => vi.advanceTimersByTime(PICTURE_RETRY_MS * 2 ** PICTURE_MAX_TRIES))
}

describe('JobPicture (#3742)', () => {
  it('shows the placeholder with no picture, and an empty alt beside the name', () => {
    const { rerender } = render(<JobPicture src={null} />)
    expect(image()).toBeNull()
    expect(screen.getByTestId('job-picture-placeholder')).toBeInTheDocument()

    rerender(<JobPicture src={A} />)
    expect(image()).toHaveAttribute('alt', '')
  })

  it('tries a failed picture again later, waiting longer each time (#5863)', () => {
    vi.useFakeTimers()
    render(<JobPicture src={A} />)

    // Bambuddy may still be taking the cover out of the file: the first load fails.
    fireEvent.error(image()!)
    expect(image()).toBeNull()
    expect(screen.getByTestId('job-picture-placeholder')).toBeInTheDocument()
    act(() => vi.advanceTimersByTime(PICTURE_RETRY_MS - 1))
    expect(image()).toBeNull()
    act(() => vi.advanceTimersByTime(1))
    expect(image()).toHaveAttribute('src', A)

    // It fails again: the next try waits twice as long.
    fireEvent.error(image()!)
    act(() => vi.advanceTimersByTime(PICTURE_RETRY_MS))
    expect(image()).toBeNull()
    act(() => vi.advanceTimersByTime(PICTURE_RETRY_MS))
    expect(image()).not.toBeNull()
  })

  it('gives a picture that always fails up after PICTURE_MAX_TRIES loads (#5877)', () => {
    vi.useFakeTimers()
    render(<JobPicture src={A} />)

    // Every load but the last is followed by one more try.
    for (let load = 1; load < PICTURE_MAX_TRIES; load++) {
      failAndWait()
      expect(image()).not.toBeNull()
    }
    // The last fails for good: the placeholder stays, and no timer asks again.
    failAndWait()
    expect(image()).toBeNull()
    act(() => vi.advanceTimersByTime(24 * 60 * 60 * 1000))
    expect(image()).toBeNull()
    expect(vi.getTimerCount()).toBe(0)
  })

  it('shows a new picture at once, whatever the last one did', () => {
    vi.useFakeTimers()
    const { rerender } = render(<JobPicture src={A} />)
    fireEvent.error(image()!)
    expect(image()).toBeNull()

    rerender(<JobPicture src={B} />)
    expect(image()).toHaveAttribute('src', B)
    // The new picture's first failure waits the first delay, not the old one's longer one.
    fireEvent.error(image()!)
    act(() => vi.advanceTimersByTime(PICTURE_RETRY_MS))
    expect(image()).not.toBeNull()
  })

  it('starts a picture that comes back afresh: A, then B, then A again (#5877)', () => {
    vi.useFakeTimers()
    const { rerender } = render(<JobPicture src={A} />)
    // A has failed every try but its last, and waits.
    for (let load = 1; load < PICTURE_MAX_TRIES; load++) failAndWait()
    fireEvent.error(image()!)
    expect(image()).toBeNull()

    rerender(<JobPicture src={B} />)
    rerender(<JobPicture src={A} />)
    // A is shown at once, with all its tries again: any change of URL starts over.
    expect(image()).toHaveAttribute('src', A)
    for (let load = 1; load < PICTURE_MAX_TRIES; load++) {
      failAndWait()
      expect(image()).not.toBeNull()
    }
  })
})
