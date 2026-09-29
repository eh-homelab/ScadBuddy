// The dollhouse designer's bill of pieces (#425): a box house on a one-module grid.
// Pure, so it is tested on its own; ui/index.js draws it.

export const LIMITS = { cols: [1, 4], rows: [1, 3], storeys: [1, 3], windows: [0, 14] }
export const DEFAULT_HOUSE = { cols: 2, rows: 1, storeys: 1, windows: 2 }

const LABELS = {
  wall: 'Wall',
  wall_window: 'Wall with window',
  wall_door_lower: 'Door wall, lower course',
  wall_door_upper: 'Door wall, upper course',
  door_leaf_lower: 'Door leaf, lower half',
  door_leaf_upper: 'Door leaf, upper half',
  corner_post: 'Corner post',
  floor_tile: 'Floor tile',
  roof_panel: 'Roof panel',
  stairs_lower: 'Stairs, lower half',
  stairs_upper: 'Stairs, upper half',
  railing: 'Railing',
  connectors: 'Connectors (keys, pegs, hinge pins)',
}

const COURSED = new Set(['wall', 'corner_post'])

export function clampHouse(house) {
  const out = {}
  for (const [key, [min, max]] of Object.entries(LIMITS)) {
    const value = Math.trunc(Number(house?.[key]))
    out[key] = Number.isFinite(value) ? Math.min(max, Math.max(min, value)) : DEFAULT_HOUSE[key]
  }
  return out
}

export function housePieces(input) {
  const { cols, rows, storeys, windows } = clampHouse(input)
  const perimeter = 2 * (cols + rows)
  const counts = new Map()
  // Walls and corner posts come in a lower and an upper course (the model renders them
  // differently), so each course is its own entry. A window wall is always upper.
  const add = (piece, course, count) => {
    if (count <= 0) return
    const coursed = COURSED.has(piece)
    const id = coursed ? `${piece}:${course}` : piece
    const entry = counts.get(id) ?? { id, piece, course, count: 0, label: '' }
    entry.count += count
    entry.label = coursed ? `${LABELS[piece]}, ${course} course` : LABELS[piece]
    counts.set(id, entry)
  }
  for (let storey = 0; storey < storeys; storey++) {
    const ground = storey === 0
    const openings = ground ? perimeter - 1 : perimeter
    const glazed = Math.min(windows, openings)
    if (ground) {
      add('wall_door_lower', null, 1)
      add('wall_door_upper', null, 1)
    }
    add('wall', 'lower', openings)
    add('wall_window', 'upper', glazed)
    add('wall', 'upper', openings - glazed)
    add('corner_post', 'lower', 4)
    add('corner_post', 'upper', 4)
    add('floor_tile', null, cols * rows)
  }
  add('roof_panel', null, cols * rows)
  add('stairs_lower', null, storeys - 1)
  add('stairs_upper', null, storeys - 1)
  add('railing', null, storeys - 1)
  add('door_leaf_lower', null, 1)
  add('door_leaf_upper', null, 1)
  add('connectors', null, 1)
  return [...counts.values()]
}

export function pieceParams(entry) {
  return {
    piece: entry.piece,
    ...(entry.course ? { course: entry.course } : {}),
    width_units: 1,
    depth_units: 1,
  }
}
