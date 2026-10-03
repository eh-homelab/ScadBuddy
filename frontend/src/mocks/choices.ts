import type { ChoicesView, PrintRunResult } from '../api/types'
import { filamentOptions, settings, targets } from './fixtures'

/**
 * spec 2026-09-27 — `GET /print/outputs/{id}/choices`, shaped like the backend's own
 * answer in `backend/tests/api/test_print_choices.py`: the H2C with a 0.2 and a High
 * Flow 0.4 installed, Bambu's three tiers per nozzle size, the processes each size
 * takes, and the filament presets Advanced mode's per-slot override picks from.
 */
export const choicesView: ChoicesView = {
  printer_id: 1,
  printers: targets.printers ?? [],
  nozzle_sizes: ['0.2', '0.4', '0.6', '0.8'],
  rack_algorithm: 'least_used',
  installed: [
    { size: '0.2', flow: 'standard', count: 1 },
    { size: '0.4', flow: 'high_flow', count: 2 },
  ],
  tiers: {
    '0.2': [
      { tier: 'fine', process_name: '0.08mm High Quality @BBL H2C 0.2 nozzle' },
      { tier: 'standard', process_name: '0.10mm Standard @BBL H2C 0.2 nozzle' },
      { tier: 'draft', process_name: '0.12mm Balanced Quality @BBL H2C 0.2 nozzle' },
    ],
    '0.4': [
      { tier: 'fine', process_name: '0.12mm High Quality @BBL H2C' },
      { tier: 'standard', process_name: '0.20mm Standard @BBL H2C' },
      { tier: 'draft', process_name: '0.24mm Standard @BBL H2C' },
    ],
    '0.6': [
      { tier: 'fine', process_name: '0.18mm Balanced Quality @BBL H2C 0.6 nozzle' },
      { tier: 'standard', process_name: '0.24mm Balanced Quality @BBL H2C 0.6 nozzle' },
      { tier: 'draft', process_name: '0.30mm Standard @BBL H2C 0.6 nozzle' },
    ],
    '0.8': [
      { tier: 'fine', process_name: '0.24mm Balanced Quality @BBL H2C 0.8 nozzle' },
      { tier: 'standard', process_name: '0.32mm Balanced Quality @BBL H2C 0.8 nozzle' },
      { tier: 'draft', process_name: '0.40mm Standard @BBL H2C 0.8 nozzle' },
    ],
  },
  processes: {
    '0.2': [
      '0.06mm Fine @BBL H2C 0.2 nozzle',
      '0.08mm High Quality @BBL H2C 0.2 nozzle',
      '0.10mm Standard @BBL H2C 0.2 nozzle',
      '0.12mm Balanced Quality @BBL H2C 0.2 nozzle',
    ],
    '0.4': ['0.12mm High Quality @BBL H2C', '0.20mm Standard @BBL H2C', '0.24mm Standard @BBL H2C'],
    '0.6': ['0.24mm Balanced Quality @BBL H2C 0.6 nozzle', '0.30mm Standard @BBL H2C 0.6 nozzle'],
    '0.8': ['0.32mm Balanced Quality @BBL H2C 0.8 nozzle', '0.40mm Standard @BBL H2C 0.8 nozzle'],
  },
  bed_types: [
    'Cool Plate',
    'Engineering Plate',
    'High Temp Plate',
    'Textured PEI Plate',
    'Supertack Plate',
  ],
  last_bed_type: 'Textured PEI Plate',
  bed_type: 'Textured PEI Plate',
  filaments: filamentOptions,
  filament_presets: {
    '0.2': [
      {
        ref: { source: 'cloud', id: 'GFSA00_23' },
        name: 'Bambu PLA Basic @BBL H2C 0.2 nozzle',
      },
      {
        ref: { source: 'local', id: '1' },
        name: 'Cookiecad PETG Magic Dark Magic (3DFP 7JdoWkaDB) @H2C 0.2n',
      },
    ],
    '0.4': [
      {
        ref: { source: 'cloud', id: 'GFSA00_22' },
        name: 'Bambu PLA Basic @BBL H2C',
      },
      {
        ref: { source: 'cloud', id: 'GFSB00_22' },
        name: 'Bambu ABS @BBL H2C',
      },
    ],
    '0.6': [],
    '0.8': [],
  },
  model_choices: { printer_id: null, filament_plan: [] },
}

/** What `POST /print/outputs/{id}/run` answers: the spool-first run only slices and queues. */
export const queuedResult: PrintRunResult = {
  route: 'slice_queue',
  library_file_id: 8812,
  printer_id: 1,
  slice_job_id: 31,
  sliced_library_file_id: 8813,
  queue_item_ids: [4472],
  copies: 1,
  warnings: [],
  project_id: null,
  folder_id: null,
  bambuddy_url: `${settings.bambuddy_url}/queue`,
}
