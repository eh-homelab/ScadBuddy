import { act, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { JobPicture, PICTURE_RETRY_MAX_MS, PICTURE_RETRY_MS } from './JobPicture'

afterEach(() => {
  vi.useRealTimers()
})

function image(): HTMLImageElement | null {
  return screen.getByTestId('job-picture').querySelector('img')
}

describe('JobPicture (#3742)', () => {
  it('shows the placeholder with no picture, and an empty alt beside the name', () => {
    const { rerender } = render(<JobPicture src={null} />)
    expect(image()).toBeNull()
    expect(screen.getByTestId('job-picture-placeholder')).toBeInTheDocument()

    rerender(<JobPicture src="/api/v1/farm/printers/1/cover?archive=90" />)
    expect(image()).toHaveAttribute('alt', '')
  })

  it('tries a failed picture again later, waiting longer each time, never in a tight loop (#5863)', () => {
    vi.useFakeTimers()
    render(<JobPicture src="/api/v1/farm/printers/1/cover?archive=90" />)

    // Bambuddy may still be taking the cover out of the file: the first load fails.
    fireEvent.error(image()!)
    expect(image()).toBeNull()
    expect(screen.getByTestId('job-picture-placeholder')).toBeInTheDocument()
    act(() => vi.advanceTimersByTime(PICTURE_RETRY_MS - 1))
    expect(image()).toBeNull()
    act(() => vi.advanceTimersByTime(1))
    expect(image()).toHaveAttribute('src', '/api/v1/farm/printers/1/cover?archive=90')

    // It fails again: the next try waits twice as long.
    fireEvent.error(image()!)
    act(() => vi.advanceTimersByTime(PICTURE_RETRY_MS))
    expect(image()).toBeNull()
    act(() => vi.advanceTimersByTime(PICTURE_RETRY_MS))
    expect(image()).not.toBeNull()

    // However often it fails, a try waits at most PICTURE_RETRY_MAX_MS.
    for (let i = 0; i < 10; i++) {
      fireEvent.error(image()!)
      act(() => vi.advanceTimersByTime(PICTURE_RETRY_MAX_MS))
      expect(image()).not.toBeNull()
    }
  })

  it('shows a new picture at once, whatever the last one did', () => {
    vi.useFakeTimers()
    const { rerender } = render(<JobPicture src="/api/v1/farm/printers/1/cover?archive=90" />)
    fireEvent.error(image()!)
    expect(image()).toBeNull()

    rerender(<JobPicture src="/api/v1/farm/printers/1/cover?archive=91" />)
    expect(image()).toHaveAttribute('src', '/api/v1/farm/printers/1/cover?archive=91')
    // The new picture's first failure waits the first delay, not the old one's longer one.
    fireEvent.error(image()!)
    act(() => vi.advanceTimersByTime(PICTURE_RETRY_MS))
    expect(image()).not.toBeNull()
  })
})
