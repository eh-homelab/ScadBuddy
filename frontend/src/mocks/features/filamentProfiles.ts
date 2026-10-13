import { HttpResponse, http } from 'msw'
import type { FilamentProfile, ProfilePreview } from '../../api/types'

/**
 * #717 — the built-in Silk PLA profile, and its preview against an H2C process whose
 * outer wall speed is one value per extruder variant (`0.20mm Standard @BBL H2C`).
 */
export const SILK_PROFILE: FilamentProfile = {
  id: 'silk-pla',
  name: 'Silk PLA',
  description:
    "Bambu's silk guide: a slower, consistent outer wall is what gives silk its gloss. It raises print time.",
  match: { material: 'PLA', word: 'silk' },
  settings: [
    {
      key: 'outer_wall_speed',
      label: 'Outer wall speed',
      value: '50',
      unit: 'mm/s',
      sources: [
        {
          url: 'https://wiki.bambulab.com/en/x1/manual/printing-with-silk-filaments',
          title: 'Bambu PLA Silk / Silk+ Filament Printing Guide | Bambu Lab Wiki',
          quote: 'Go to Process → Speed and set Outer wall to 40–60 mm/s. Try 50 mm/s first.',
          accessed: '2026-09-28',
          supports: ['SB2001', 'outer_wall_speed'],
        },
      ],
    },
  ],
  notes: [
    {
      text: "Bambu also recommends a 235 °C nozzle for silk. ScadBuddy cannot set a filament's temperature yet, so it is not changed here: set it in the spool's filament preset.",
      sources: [],
    },
  ],
  builtin: true,
}

export function silkPreview(overrides: Partial<ProfilePreview> = {}): ProfilePreview {
  return {
    profile: SILK_PROFILE,
    process_preset: { source: 'cloud', id: 'GP247' },
    applicable: true,
    reason: null,
    changes: [
      {
        key: 'outer_wall_speed',
        label: 'Outer wall speed',
        unit: 'mm/s',
        current: ['200', '500', '500', '200', '500'],
        value: '50',
      },
    ],
    ...overrides,
  }
}

let preview: ProfilePreview = silkPreview()
export function reset(): void {
  preview = silkPreview()
}

export function setSilkPreview(next: ProfilePreview): void {
  preview = next
}

export const handlers = [
  http.get('/api/v1/print/filament-profiles', () => HttpResponse.json([SILK_PROFILE])),
  http.post('/api/v1/print/filament-profiles/preview', async ({ request }) => {
    const { profiles } = (await request.json()) as { profiles?: string[] }
    return HttpResponse.json((profiles ?? ['silk-pla']).includes('silk-pla') ? [preview] : [])
  }),
]
