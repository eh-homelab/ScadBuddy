import type { CatalogKind } from '../../api/filament'

type FieldKind = 'text' | 'int' | 'decimal' | 'bool' | 'ref' | 'rgba' | 'rgbalist'

export interface Field {
  name: string
  label: string
  kind: FieldKind
  /** A `ref` field's catalog. */
  ref?: CatalogKind
  /** The route refuses a row without it. */
  required?: boolean
}

const text = (name: string, label: string): Field => ({ name, label, kind: 'text' })
const int = (name: string, label: string, required = false): Field => ({ name, label, kind: 'int', required })
const decimal = (name: string, label: string): Field => ({ name, label, kind: 'decimal' })
const bool = (name: string, label: string): Field => ({ name, label, kind: 'bool' })
const ref = (name: string, label: string, to: CatalogKind): Field => ({ name, label, kind: 'ref', ref: to })

const TEMPS = [
  decimal('density_g_cm3', 'Density (g/cm³)'),
  int('nozzle_temp_min', 'Nozzle min (°C)'),
  int('nozzle_temp_max', 'Nozzle max (°C)'),
  int('bed_temp_min', 'Bed min (°C)'),
  int('bed_temp_max', 'Bed max (°C)'),
  int('drying_temp_c', 'Drying (°C)'),
  decimal('drying_hours', 'Drying (hours)'),
]

/**
 * The catalogs (`backend/scadbuddy/filament/catalog.py` `CATALOGS`), in the order the tab
 * lists them, with the value columns each takes beyond `name` and `sort_order`
 * (`filament/models.py`'s `*Create` models). A key outside them is a 422.
 */
export const CATALOGS: { kind: CatalogKind; title: string; fields: Field[] }[] = [
  {
    kind: 'materials',
    title: 'Materials',
    fields: [
      text('family', 'Family'),
      ...TEMPS,
      bool('needs_enclosure', 'Needs an enclosure'),
      bool('abrasive', 'Abrasive'),
      bool('hygroscopic', 'Hygroscopic'),
      bool('flexible', 'Flexible'),
      bool('hardened_nozzle', 'Needs a hardened nozzle'),
      text('note', 'Note'),
    ],
  },
  { kind: 'subtypes', title: 'Subtypes', fields: [ref('material_id', 'Material', 'materials'), ...TEMPS] },
  { kind: 'brands', title: 'Brands', fields: [] },
  { kind: 'categories', title: 'Categories', fields: [] },
  { kind: 'effects', title: 'Effects', fields: [] },
  { kind: 'insert-types', title: 'Insert types', fields: [int('empty_weight_g', 'Empty weight (g)', true), ref('insert_material_id', 'Insert material', 'insert-materials'), text('note', 'Note')] },
  { kind: 'insert-materials', title: 'Insert materials', fields: [] },
  {
    kind: 'location-kinds',
    title: 'Location kinds',
    fields: [bool('dry_storage', 'Dry storage'), bool('ams', 'AMS tray')],
  },
  {
    kind: 'colours',
    title: 'Colours',
    fields: [text('manufacturer', 'Manufacturer'), { name: 'rgba', label: 'Colour', kind: 'rgba', required: true }, { name: 'extra_colours', label: 'Extra colours', kind: 'rgbalist' }, ref('material_id', 'Material', 'materials'), ref('effect_id', 'Effect', 'effects')],
  },
]
