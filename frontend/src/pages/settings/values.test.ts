import { describe, expect, it } from 'vitest'
import type { Settings } from '../../api/types'
import { mockSettings } from '../../mocks/handlers'
import { baseline, fieldsOf, ofLimit, storeChoice, toPatchValue } from './values'

const settings = mockSettings()

describe('settings values', () => {
  it('turns retention days into seconds, empty into null, and refuses under a day', () => {
    expect(toPatchValue('print_run_retention_seconds', '2', settings)).toBe(172800)
    expect(toPatchValue('print_run_retention_seconds', '', settings)).toBeNull()
    expect(toPatchValue('print_run_retention_seconds', '0.5', settings)).toEqual({
      problem: 'Enter 1 to 3650 days, or leave it empty to keep every one.',
    })
  })
  it('reads an empty follow list as "none" and a missing one as empty', () => {
    expect(baseline({ ...settings, activity_follow: [] }, 'activity_follow')).toBe('none')
    expect(baseline({ ...settings, activity_follow: null }, 'activity_follow')).toBe('')
  })
  it('lists a section’s hand-laid fields first, then its runtime ones', () => {
    expect(fieldsOf('connection', settings)).toEqual([
      'bambuddy_url',
      'bambuddy_web_urls',
      'bambuddy_api_key',
      'bambuddy_render_api_key',
      'public_url',
    ])
    expect(fieldsOf('fonts', settings)).toEqual(['google_fonts_api_key', 'fonts_catalogue_ttl'])
  })
  it('puts an env-seeded field with no control in Diagnostics (#322)', () => {
    const extra = { ...settings, sources: { ...settings.sources, made_up_knob: 'env' } } as Settings
    expect(fieldsOf('diagnostics', extra)).toContain('made_up_knob')
  })
  it('chooses the S3 store only once it is saved, and never the Bambuddy library (#7257)', () => {
    const s3 = (n: string) => (n === 'store_backend' ? 's3' : '')
    expect(storeChoice(settings, s3).backend).toBe('local')
    const saved = {
      ...settings,
      store_s3_endpoint: 'https://s3.test',
      store_s3_bucket: 'outputs',
      store_s3_access_key_id: 'key',
      has_store_s3_secret_access_key: true,
    }
    expect(storeChoice(saved, s3).backend).toBe('s3')
    const bambuddy = { ...saved, bambuddy_url: 'https://b.test', library_folder_id: 3 }
    expect(storeChoice(bambuddy, (n) => (n === 'store_backend' ? 'bambuddy' : '3')).backend).toBe('local')
  })
  it('says a limit of 0 is none', () => {
    expect(ofLimit('3', 0, String)).toBe('3 (no limit)')
    expect(ofLimit('3', 10, String)).toBe('3 of 10')
  })
})
