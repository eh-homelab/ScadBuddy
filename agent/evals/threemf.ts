import { crc32, deflateRawSync, inflateRawSync } from 'node:zlib'

// Just enough of a 3MF reader to check a render the way models/<name>/verify.sh
// does (issue #1924, docs/ai/evals.md "Real-render checks"), without a new
// dependency: a ZIP reader (APPNOTE.TXT §4.3: the end of central directory
// record, the central directory, then each local header; methods 0 and 8 only)
// and the parts of ScadBuddy's Bambu-style 3MF
// (backend/scadbuddy/render/bambu3mf.py): one mesh per colour in
// 3D/Objects/object_<n>.model, each part's extruder in
// Metadata/model_settings.config, and the extruders' colours in
// Metadata/project_settings.config `filament_colour`. The writer exists so the
// scripted run's recorded backend can serve a 3MF of the same shape.

const EOCD = 0x06054b50
const CENTRAL = 0x02014b50
const LOCAL = 0x04034b50

/** The entries of a ZIP archive by name. Throws on anything it cannot read. */
export function readZip(zip: Buffer): Map<string, Buffer> {
  let end = -1
  for (let i = zip.length - 22; i >= Math.max(0, zip.length - 22 - 0xffff); i--) {
    if (zip.readUInt32LE(i) === EOCD) {
      end = i
      break
    }
  }
  if (end < 0) throw new Error('not a ZIP archive (no end of central directory)')
  const count = zip.readUInt16LE(end + 10)
  let at = zip.readUInt32LE(end + 16)
  const entries = new Map<string, Buffer>()
  for (let n = 0; n < count; n++) {
    if (zip.readUInt32LE(at) !== CENTRAL) throw new Error('bad ZIP central directory')
    const method = zip.readUInt16LE(at + 10)
    const size = zip.readUInt32LE(at + 20)
    const nameLength = zip.readUInt16LE(at + 28)
    const extraLength = zip.readUInt16LE(at + 30)
    const commentLength = zip.readUInt16LE(at + 32)
    const local = zip.readUInt32LE(at + 42)
    const name = zip.toString('utf8', at + 46, at + 46 + nameLength)
    at += 46 + nameLength + extraLength + commentLength
    if (zip.readUInt32LE(local) !== LOCAL) throw new Error(`bad ZIP local header for ${name}`)
    const start = local + 30 + zip.readUInt16LE(local + 26) + zip.readUInt16LE(local + 28)
    const data = zip.subarray(start, start + size)
    if (method === 0) entries.set(name, Buffer.from(data))
    else if (method === 8) entries.set(name, inflateRawSync(data))
    else throw new Error(`${name}: ZIP method ${method} is not supported`)
  }
  return entries
}

/** A ZIP archive of the given entries, deflated when that is smaller. */
export function writeZip(files: Record<string, string | Buffer>): Buffer {
  const locals: Buffer[] = []
  const centrals: Buffer[] = []
  let offset = 0
  for (const [name, content] of Object.entries(files)) {
    const raw = typeof content === 'string' ? Buffer.from(content) : content
    const deflated = deflateRawSync(raw)
    const method = deflated.length < raw.length ? 8 : 0
    const data = method === 8 ? deflated : raw
    const fileName = Buffer.from(name)
    const header = Buffer.alloc(30)
    header.writeUInt32LE(LOCAL, 0)
    header.writeUInt16LE(20, 4)
    header.writeUInt16LE(method, 8)
    header.writeUInt32LE(crc32(raw), 14)
    header.writeUInt32LE(data.length, 18)
    header.writeUInt32LE(raw.length, 22)
    header.writeUInt16LE(fileName.length, 26)
    const central = Buffer.alloc(46)
    central.writeUInt32LE(CENTRAL, 0)
    central.writeUInt16LE(20, 4)
    central.writeUInt16LE(20, 6)
    central.writeUInt16LE(method, 10)
    central.writeUInt32LE(crc32(raw), 16)
    central.writeUInt32LE(data.length, 20)
    central.writeUInt32LE(raw.length, 24)
    central.writeUInt16LE(fileName.length, 28)
    central.writeUInt32LE(offset, 42)
    locals.push(header, fileName, data)
    centrals.push(central, fileName)
    offset += header.length + fileName.length + data.length
  }
  const directory = Buffer.concat(centrals)
  const end = Buffer.alloc(22)
  end.writeUInt32LE(EOCD, 0)
  end.writeUInt16LE(centrals.length / 2, 8)
  end.writeUInt16LE(centrals.length / 2, 10)
  end.writeUInt32LE(directory.length, 12)
  end.writeUInt32LE(offset, 16)
  return Buffer.concat([...locals, directory, end])
}

export type Part = {
  /** The archive entry, `3D/Objects/object_<n>.model`. */
  file: string
  objectId: string
  extruder: number
  /** `filament_colour[extruder - 1]`, as written (`#RRGGBB`). */
  colour: string
  triangles: number
  zMin: number
  zMax: number
}

export type Rendered3mf = { parts: Part[]; zMin: number; zMax: number }

function attributes(tag: string): Record<string, string> {
  const out: Record<string, string> = {}
  for (const m of tag.matchAll(/([\w:-]+)="([^"]*)"/g)) out[m[1]!] = m[2]!
  return out
}

/** Each part's extruder, by object id, from Metadata/model_settings.config. */
function extruders(config: string): Map<string, number> {
  const out = new Map<string, number>()
  for (const part of config.matchAll(/<part\b([^>]*)>([\s\S]*?)<\/part>/g)) {
    const id = attributes(part[1]!).id
    const extruder = /<metadata\s+key="extruder"\s+value="(\d+)"/.exec(part[2]!)?.[1]
    if (id !== undefined && extruder !== undefined) out.set(id, Number(extruder))
  }
  return out
}

/** Reads ScadBuddy's Bambu-style 3MF (bambu3mf.py): its parts, their colours and heights. */
export function readBambu3mf(zip: Buffer): Rendered3mf {
  const entries = readZip(zip)
  const settings = entries.get('Metadata/project_settings.config')
  const modelSettings = entries.get('Metadata/model_settings.config')
  if (!settings) throw new Error('no Metadata/project_settings.config')
  if (!modelSettings) throw new Error('no Metadata/model_settings.config')
  const colours = (JSON.parse(settings.toString('utf8')) as { filament_colour?: unknown }).filament_colour
  if (!Array.isArray(colours)) throw new Error('project_settings.config has no filament_colour list')
  const byObject = extruders(modelSettings.toString('utf8'))
  const parts: Part[] = []
  for (const [file, content] of [...entries].sort(([a], [b]) => a.localeCompare(b, 'en', { numeric: true }))) {
    if (!/^3D\/Objects\/[^/]+\.model$/.test(file)) continue
    const xml = content.toString('utf8')
    const objectId = attributes(/<object\b[^>]*>/.exec(xml)?.[0] ?? '').id ?? ''
    // A loop, not Math.min(...zs): a real render has more vertices than a call takes arguments.
    let zMin = NaN
    let zMax = NaN
    for (const m of xml.matchAll(/<vertex\b[^>]*\bz="([^"]+)"/g)) {
      const z = Number(m[1])
      if (!(z >= zMin)) zMin = z
      if (!(z <= zMax)) zMax = z
    }
    const extruder = byObject.get(objectId) ?? 0
    parts.push({
      file,
      objectId,
      extruder,
      colour: String(colours[extruder - 1] ?? ''),
      triangles: xml.match(/<triangle\b/g)?.length ?? 0,
      zMin,
      zMax,
    })
  }
  const filled = parts.filter((p) => p.triangles > 0)
  return {
    parts,
    zMin: filled.length ? Math.min(...filled.map((p) => p.zMin)) : NaN,
    zMax: filled.length ? Math.max(...filled.map((p) => p.zMax)) : NaN,
  }
}
