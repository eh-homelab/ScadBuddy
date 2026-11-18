import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { lastSupplier, readQuery, remember, rememberedQuery, rememberSupplier, setRemembering, writeQuery } from './invoicesQuery'

beforeEach(() => window.localStorage.clear())
afterEach(() => vi.restoreAllMocks())

describe('the invoices query', () => {
  it('reads the status and supplier from the address, and writes only what is not the default', () => {
    expect(readQuery(new URLSearchParams(''))).toEqual({ status: 'all' })
    expect(readQuery(new URLSearchParams('status=draft&supplier=abc'))).toEqual({ status: 'draft', supplier: 'abc' })
    expect(readQuery(new URLSearchParams('status=bogus'))).toEqual({ status: 'all' })
    expect(writeQuery({ status: 'all' }).toString()).toBe('')
    expect(writeQuery({ status: 'confirmed', supplier: 'abc' }).toString()).toBe('status=confirmed&supplier=abc')
  })

  it('remembers the view only while "Remember my view" is on', () => {
    expect(rememberedQuery()).toBeNull()
    remember({ status: 'draft' })
    expect(rememberedQuery()).toBeNull()
    setRemembering(true, { status: 'confirmed', supplier: 's1' })
    expect(rememberedQuery()).toEqual({ status: 'confirmed', supplier: 's1' })
    remember({ status: 'draft' })
    expect(rememberedQuery()).toEqual({ status: 'draft' })
    setRemembering(false, { status: 'draft' })
    expect(rememberedQuery()).toBeNull()
  })

  it('remembers the last supplier', () => {
    expect(lastSupplier()).toBeNull()
    rememberSupplier('s9')
    expect(lastSupplier()).toBe('s9')
  })

  it('works when storage throws', () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('denied')
    })
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('denied')
    })
    expect(rememberedQuery()).toBeNull()
    expect(lastSupplier()).toBeNull()
    expect(() => remember({ status: 'draft' })).not.toThrow()
    expect(() => setRemembering(true, { status: 'draft' })).not.toThrow()
    expect(() => rememberSupplier('x')).not.toThrow()
  })
})
