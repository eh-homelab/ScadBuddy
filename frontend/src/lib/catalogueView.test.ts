import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  CATALOGUE_VIEW_KEY,
  readStoredView,
  resetStoredView,
  storeView,
} from './catalogueView'

afterEach(() => {
  vi.restoreAllMocks()
  window.localStorage.clear()
  resetStoredView()
})

describe('the remembered catalogue view (#278)', () => {
  it('is nothing until one is chosen', () => {
    expect(readStoredView()).toBeNull()
  })

  it('keeps the chosen view in localStorage', () => {
    storeView('list')
    expect(window.localStorage.getItem(CATALOGUE_VIEW_KEY)).toBe('list')
    expect(readStoredView()).toBe('list')
    storeView('cards')
    expect(readStoredView()).toBe('cards')
  })

  it('ignores a stored value that is not a view', () => {
    window.localStorage.setItem(CATALOGUE_VIEW_KEY, 'grid')
    expect(readStoredView()).toBeNull()
  })

  it('falls back to memory for this page when storage throws', () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new DOMException('blocked', 'SecurityError')
    })
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new DOMException('blocked', 'SecurityError')
    })
    expect(readStoredView()).toBeNull()
    expect(() => storeView('list')).not.toThrow()
    expect(readStoredView()).toBe('list')
  })
})
