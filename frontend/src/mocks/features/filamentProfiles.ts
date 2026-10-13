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
        {
          url: 'https://github.com/bambulab/BambuStudio/blob/f977235e6d736c4c0b650520ac5a5b72cbfe9244/src/libslic3r/PrintConfig.cpp#L2245-L2249',
          title: 'Bambu Studio PrintConfig.cpp: outer_wall_speed',
          quote:
            "Speed of outer wall which is outermost and visible. It's used to be slower than inner wall speed to get better quality.",
          accessed: '2026-09-28',
          supports: ['outer_wall_speed'],
        },
      ],
    },
  ],
  notes: [
    {
      text: "Bambu also recommends a 235 °C nozzle for silk. ScadBuddy cannot set a filament's temperature yet, so it is not changed here: set it in the spool's filament preset.",
      sources: [
        {
          url: 'https://wiki.bambulab.com/en/x1/manual/printing-with-silk-filaments',
          title: 'Bambu PLA Silk / Silk+ Filament Printing Guide | Bambu Lab Wiki',
          quote: 'We recommend setting the Nozzle first layer and other layers to 235℃.',
          accessed: '2026-09-28',
          supports: ['SB2001', 'nozzle_temperature', 'nozzle_temperature_initial_layer'],
        },
      ],
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
