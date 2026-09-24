import type {
  BambuddyTargets,
  BoundingBox,
  CatalogueFont,
  CustomizerSchema,
  FontFamily,
  ModelSummary,
  Output,
  Param,
  Settings,
} from '../api/types'

/**
 * `group` carries a default on the wire, so the generated type makes it required —
 * every fixture parameter really does arrive with one.
 */
function param(group: string, rest: Omit<Param, 'group'>): Param {
  return { group, ...rest }
}

/** A bounding box as the API reports it: corners plus the size, not a bare `{x,y,z}`. */
export function bbox(x: number, y: number, z: number): BoundingBox {
  return { min: [-x / 2, -y / 2, 0], max: [x / 2, y / 2, z], size: [x, y, z] }
}

export const keychainSchema: CustomizerSchema = {
  title: 'Name Keychain',
  source_sha256: 'f'.repeat(64),
  groups: ['Text', 'Plate', 'Colours'],
  parameters: [
    param('Text', {
      name: 'name',
      type: 'string',
      initial: 'Reagan',
      caption: 'Name on the tag',
      max_length: 20,
    }),
    param('Text', { name: 'font', type: 'font', initial: 'Liberation Sans:style=Bold', caption: 'Typeface' }),
    param('Text', {
      name: 'text_size',
      type: 'slider',
      initial: 14,
      caption: 'Text size',
      min: 6,
      max: 28,
      step: 0.5,
    }),
    param('Text', {
      name: 'text_depth',
      type: 'slider',
      initial: 1.6,
      caption: 'Raised height',
      min: 0.4,
      max: 4,
      step: 0.2,
    }),
    param('Plate', {
      name: 'thickness',
      type: 'slider',
      initial: 5.2,
      caption: 'Plate thickness',
      min: 2,
      max: 10,
      step: 0.2,
    }),
    param('Plate', { name: 'padding', type: 'number', initial: 6, caption: 'Margin around the text' }),
    param('Plate', { name: 'corner_radius', type: 'integer', initial: 4, caption: 'Corner radius' }),
    param('Plate', { name: 'keyring_hole', type: 'boolean', initial: true, caption: 'Keyring hole' }),
    param('Plate', {
      name: 'hole_side',
      type: 'select',
      initial: 'left',
      caption: 'Hole position',
      options: [
        { name: 'Left', value: 'left' },
        { name: 'Right', value: 'right' },
        { name: 'Top centre', value: 'top' },
      ],
    }),
    param('Colours', { name: 'body_color', type: 'color', initial: '#1B6CA8', caption: 'Plate' }),
    param('Colours', { name: 'text_color', type: 'color', initial: '#E8532F', caption: 'Text' }),
  ],
}

export const gridfinitySchema: CustomizerSchema = {
  title: 'Gridfinity Bin',
  source_sha256: 'a'.repeat(64),
  groups: ['Size', 'Features'],
  parameters: [
    param('Size', { name: 'units_x', type: 'integer', initial: 2, caption: 'Width in grid units' }),
    param('Size', { name: 'units_y', type: 'integer', initial: 1, caption: 'Depth in grid units' }),
    param('Size', {
      name: 'height_units',
      type: 'slider',
      initial: 3,
      caption: 'Height in 7 mm units',
      min: 1,
      max: 12,
      step: 1,
    }),
    param('Features', { name: 'magnets', type: 'boolean', initial: false, caption: 'Magnet holes' }),
    param('Features', { name: 'label_tab', type: 'boolean', initial: true, caption: 'Label tab' }),
    param('Features', { name: 'bin_color', type: 'color', initial: '#2E7D5B', caption: 'Bin' }),
  ],
}

export const models: ModelSummary[] = [
  {
    slug: 'name-keychain',
    name: 'Name Keychain',
    description: 'Two-colour keychain with raised text. The reference model for ScadBuddy.',
    tags: ['keychain', 'two-colour', 'text'],
    updated_at: '2026-09-21T18:04:00Z',
    has_thumbnail: true,
    has_readme: true,
  },
  {
    slug: 'gridfinity-bin',
    name: 'Gridfinity Bin',
    description: 'Parametric storage bin on the 42 mm Gridfinity grid.',
    tags: ['storage', 'gridfinity'],
    updated_at: '2026-09-14T09:12:00Z',
    has_thumbnail: false,
    has_readme: false,
  },
]

export const schemas: Record<string, CustomizerSchema> = {
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

/**
 * A slice of the Google Fonts catalogue. `Pacifico` is the acceptance case from issue
 * #82: not in the image, so picking it has to install it first.
 */
export const fontCatalogue: CatalogueFont[] = [
  {
    family: 'Roboto',
    category: 'sans-serif',
    popularity: 1,
    installed: false,
    variants: [
      { weight: 400, italic: false },
      { weight: 700, italic: false },
    ],
  },
  {
    family: 'Noto Sans',
    category: 'sans-serif',
    popularity: 2,
    installed: true,
    variants: [{ weight: 400, italic: false }],
  },
  {
    family: 'Pacifico',
    category: 'handwriting',
    popularity: 3,
    installed: false,
    variants: [{ weight: 400, italic: false }],
  },
  {
    family: 'Lobster Two',
    category: 'display',
    popularity: 4,
    installed: false,
    variants: [
      { weight: 400, italic: false },
      { weight: 700, italic: false },
    ],
  },
  {
    family: 'Playfair Display',
    category: 'serif',
    popularity: 5,
    installed: false,
    variants: [{ weight: 400, italic: false }],
  },
]

/** The family the install route refuses, so the widget's error path is reachable. */
export const UNINSTALLABLE_FONT = 'Playfair Display'

export const outputs: Output[] = [
  {
    id: 'a'.repeat(32),
    slug: 'name-keychain',
    name: 'Reagan',
    job_id: 'b'.repeat(32),
    created_at: '2026-09-21T19:31:00Z',
    has_thumbnail: true,
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
    bbox_mm: bbox(95.7, 34.6, 6.8),
    colors: ['#1B6CA8', '#E8532F'],
    parts: [],
    warnings: [],
    library_file_id: 8812,
    queue_item_id: 4471,
  },
  {
    id: 'c'.repeat(32),
    slug: 'name-keychain',
    name: 'Nova',
    job_id: 'd'.repeat(32),
    created_at: '2026-09-20T11:22:00Z',
    has_thumbnail: false,
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
    bbox_mm: bbox(78.4, 40.2, 7.2),
    colors: ['#F2A93B', '#14181F'],
    parts: [],
    warnings: [],
    library_file_id: 8790,
  },
  {
    id: 'e'.repeat(32),
    slug: 'name-keychain',
    name: 'Workshop',
    job_id: 'f'.repeat(32),
    created_at: '2026-09-18T09:03:00Z',
    has_thumbnail: false,
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
    bbox_mm: bbox(104.1, 30.8, 5.6),
    colors: ['#1B6CA8', '#E8532F'],
    parts: [],
    warnings: [],
  },
]

export const settings: Settings = {
  bambuddy_url: 'https://bambuddy.internal.nullreference.io',
  has_api_key: true,
  public_url: 'https://scadbuddy.internal.nullreference.io',
  library_folder_id: 2,
  pipeline_id: 1,
  printer_id: 1,
  printer_preset: null,
  process_preset: null,
  filament_presets: [],
  bed_type: null,
}

export const targets: BambuddyTargets = {
  folders: [
    { id: 1, name: 'MakerWorld' },
    { id: 2, name: 'ScadBuddy' },
    { id: 3, name: 'Keychains' },
  ],
  pipelines: [
    { id: 1, name: 'Textured PEI · 0.20 mm · AMS' },
    { id: 2, name: 'Draft · 0.28 mm' },
  ],
  printers: [{ id: 1, name: '3DP-31B-598', model: 'H2C', is_active: true, nozzle_count: 2 }],
}

export const FAILING_NAME = 'boom'

export const OPENSCAD_LOG_TAIL = [
  'Compiling design (CSG Tree generation)...',
  'ERROR: Parser error: syntax error in file model.scad, line 42',
  'ERROR: Compilation failed!',
  'WARNING: Object may not be a valid 2-manifold and may need repair!',
  'Execution aborted',
]
