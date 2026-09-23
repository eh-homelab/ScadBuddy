import type { FontFamily, ModelSchema, ModelSummary, Output, Settings } from '../api/types'
import type { BambuddyTargets } from '../api/types'

export const keychainSchema: ModelSchema = {
  title: 'Name Keychain',
  groups: [
    {
      name: 'Text',
      params: [
        {
          name: 'name',
          type: 'string',
          initial: 'Reagan',
          caption: 'Name on the tag',
          maxLength: 20,
        },
        {
          name: 'font',
          type: 'font',
          initial: 'Liberation Sans:style=Bold',
          caption: 'Typeface',
        },
        {
          name: 'text_size',
          type: 'slider',
          initial: 14,
          caption: 'Text size',
          min: 6,
          max: 28,
          step: 0.5,
        },
        {
          name: 'text_depth',
          type: 'slider',
          initial: 1.6,
          caption: 'Raised height',
          min: 0.4,
          max: 4,
          step: 0.2,
        },
      ],
    },
    {
      name: 'Plate',
      params: [
        {
          name: 'thickness',
          type: 'slider',
          initial: 5.2,
          caption: 'Plate thickness',
          min: 2,
          max: 10,
          step: 0.2,
        },
        {
          name: 'padding',
          type: 'number',
          initial: 6,
          caption: 'Margin around the text',
        },
        { name: 'corner_radius', type: 'integer', initial: 4, caption: 'Corner radius' },
        { name: 'keyring_hole', type: 'boolean', initial: true, caption: 'Keyring hole' },
        {
          name: 'hole_side',
          type: 'select',
          initial: 'left',
          caption: 'Hole position',
          options: [
            { name: 'Left', value: 'left' },
            { name: 'Right', value: 'right' },
            { name: 'Top centre', value: 'top' },
          ],
        },
      ],
    },
    {
      name: 'Colours',
      params: [
        { name: 'body_color', type: 'color', initial: '#1B6CA8', caption: 'Plate' },
        { name: 'text_color', type: 'color', initial: '#E8532F', caption: 'Text' },
      ],
    },
  ],
}

export const gridfinitySchema: ModelSchema = {
  title: 'Gridfinity Bin',
  groups: [
    {
      name: 'Size',
      params: [
        { name: 'units_x', type: 'integer', initial: 2, caption: 'Width in grid units' },
        { name: 'units_y', type: 'integer', initial: 1, caption: 'Depth in grid units' },
        {
          name: 'height_units',
          type: 'slider',
          initial: 3,
          caption: 'Height in 7 mm units',
          min: 1,
          max: 12,
          step: 1,
        },
      ],
    },
    {
      name: 'Features',
      params: [
        { name: 'magnets', type: 'boolean', initial: false, caption: 'Magnet holes' },
        { name: 'label_tab', type: 'boolean', initial: true, caption: 'Label tab' },
        { name: 'bin_color', type: 'color', initial: '#2E7D5B', caption: 'Bin' },
      ],
    },
  ],
}

export const models: ModelSummary[] = [
  {
    slug: 'name-keychain',
    name: 'Name Keychain',
    description: 'Two-colour keychain with raised text. The reference model for ScadBuddy.',
    tags: ['keychain', 'two-colour', 'text'],
    updated_at: '2026-09-21T18:04:00Z',
    last_generated_at: '2026-09-21T19:31:00Z',
    output_count: 3,
  },
  {
    slug: 'gridfinity-bin',
    name: 'Gridfinity Bin',
    description: 'Parametric storage bin on the 42 mm Gridfinity grid.',
    tags: ['storage', 'gridfinity'],
    updated_at: '2026-09-14T09:12:00Z',
    output_count: 0,
  },
]

export const schemas: Record<string, ModelSchema> = {
  'name-keychain': keychainSchema,
  'gridfinity-bin': gridfinitySchema,
}

export const fonts: FontFamily[] = [
  { family: 'Liberation Sans', styles: ['Regular', 'Bold', 'Italic', 'Bold Italic'] },
  { family: 'Liberation Serif', styles: ['Regular', 'Bold'] },
  { family: 'DejaVu Sans', styles: ['Book', 'Bold', 'Oblique'] },
  { family: 'DejaVu Sans Mono', styles: ['Book', 'Bold'] },
  { family: 'Noto Sans', styles: ['Regular', 'Bold'] },
]

export const outputs: Output[] = [
  {
    id: 'out-20260921-1931',
    slug: 'name-keychain',
    created_at: '2026-09-21T19:31:00Z',
    params: {
      name: 'Reagan',
      font: 'Liberation Sans:style=Bold',
      text_size: 14,
      text_depth: 1.6,
      thickness: 5.2,
      padding: 6,
      corner_radius: 4,
      keyring_hole: true,
      hole_side: 'left',
      body_color: '#1B6CA8',
      text_color: '#E8532F',
    },
    bbox_mm: { x: 95.7, y: 34.6, z: 6.8 },
    colors: ['#1B6CA8', '#E8532F'],
    library_file_id: 'lib-8812',
    queue_item_id: 'q-4471',
  },
  {
    id: 'out-20260920-1122',
    slug: 'name-keychain',
    created_at: '2026-09-20T11:22:00Z',
    params: {
      name: 'Nova',
      font: 'Liberation Sans:style=Bold',
      text_size: 18,
      text_depth: 2,
      thickness: 5.2,
      padding: 6,
      corner_radius: 4,
      keyring_hole: true,
      hole_side: 'top',
      body_color: '#F2A93B',
      text_color: '#14181F',
    },
    bbox_mm: { x: 78.4, y: 40.2, z: 7.2 },
    colors: ['#F2A93B', '#14181F'],
    library_file_id: 'lib-8790',
  },
  {
    id: 'out-20260918-0903',
    slug: 'name-keychain',
    created_at: '2026-09-18T09:03:00Z',
    params: {
      name: 'Workshop',
      font: 'DejaVu Sans:style=Bold',
      text_size: 12,
      text_depth: 1.6,
      thickness: 4,
      padding: 5,
      corner_radius: 2,
      keyring_hole: false,
      hole_side: 'left',
      body_color: '#1B6CA8',
      text_color: '#E8532F',
    },
    bbox_mm: { x: 104.1, y: 30.8, z: 5.6 },
    colors: ['#1B6CA8', '#E8532F'],
  },
]

export const settings: Settings = {
  bambuddy_url: 'https://bambuddy.internal.nullreference.io',
  api_key_set: true,
  library_folder_id: 'folder-scadbuddy',
  pipeline_id: 'pipeline-textured-pei',
  sidebar_registered: false,
}

export const targets: BambuddyTargets = {
  folders: [
    { id: 'folder-root', name: 'Library root' },
    { id: 'folder-scadbuddy', name: 'ScadBuddy' },
    { id: 'folder-keychains', name: 'Keychains' },
  ],
  pipelines: [
    { id: 'pipeline-textured-pei', name: 'Textured PEI · 0.20 mm · AMS' },
    { id: 'pipeline-draft', name: 'Draft · 0.28 mm' },
  ],
}

export const FAILING_NAME = 'boom'

export const OPENSCAD_LOG_TAIL = `Compiling design (CSG Tree generation)...
ERROR: Parser error: syntax error in file model.scad, line 42
ERROR: Compilation failed!
WARNING: Object may not be a valid 2-manifold and may need repair!
Execution aborted`
