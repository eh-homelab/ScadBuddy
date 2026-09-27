import { describe, expect, it } from 'vitest'
import {
  classifyFiles,
  droppedFiles,
  folderOf,
  readMetaName,
  thumbnailProblem,
  uploadFilename,
} from './modelFolder'

const file = (name: string, body = 'x', type = '') => new File([body], name, { type })

function inFolder(folder: string, name: string, body = 'x'): File {
  const made = file(name, body)
  Object.defineProperty(made, 'webkitRelativePath', { value: `${folder}/${name}` })
  return made
}

describe('classifyFiles', () => {
  it('sorts a bundled model directory into the parts of an upload', () => {
    const scad = file('model.scad')
    const meta = file('model.json')
    const thumbnail = file('thumbnail.png')
    const readme = file('README.md')
    const verify = file('verify.sh')

    const { files, ignored } = classifyFiles([verify, readme, thumbnail, meta, scad], 'widget')

    expect(files).toEqual({ scad, meta, thumbnail, readme, folder: 'widget' })
    expect(ignored).toEqual([{ name: 'verify.sh' }])
  })

  it('prefers the bundled names when there is more than one candidate', () => {
    const helper = file('helper.scad')
    const scad = file('model.scad')
    const photo = file('photo.png')
    const thumbnail = file('thumbnail.png')

    const { files, ignored } = classifyFiles([helper, photo, scad, thumbnail])

    expect(files?.scad).toBe(scad)
    expect(files?.thumbnail).toBe(thumbnail)
    expect(ignored).toEqual([
      { name: 'helper.scad', reason: 'source is model.scad' },
      { name: 'photo.png', reason: 'thumbnail is thumbnail.png' },
    ])
  })

  it('takes a lone .scad on its own', () => {
    const scad = file('Vase Mode.scad')
    expect(classifyFiles([scad]).files).toEqual({ scad, folder: undefined })
  })

  it('has nothing to upload without a source', () => {
    const { files, ignored } = classifyFiles([file('thumbnail.png'), file('model.stl')])
    expect(files).toBeNull()
    // Sorted by name, like everything else here.
    expect(ignored).toEqual([{ name: 'model.stl' }, { name: 'thumbnail.png' }])
  })

  it('does not take any .json as the metadata', () => {
    const { files, ignored } = classifyFiles([file('model.scad'), file('package.json')])
    expect(files?.meta).toBeUndefined()
    expect(ignored).toEqual([{ name: 'package.json' }])
  })
})

describe('classifyFiles with no preferred name among several', () => {
  const names = ['zebra.png', 'Cover.png', 'alpha.png', 'notes.md', 'CHANGES.md', 'model.scad']
  const set = () => names.map((name) => file(name))

  it('picks the same thumbnail and README whatever order the files came in', () => {
    const forward = classifyFiles(set())
    const backward = classifyFiles(set().reverse())
    const shuffled = classifyFiles([...set().slice(3), ...set().slice(0, 3)])

    for (const result of [forward, backward, shuffled]) {
      // Code-unit order: capitals first, whatever the locale.
      expect(result.files?.thumbnail?.name).toBe('Cover.png')
      expect(result.files?.readme?.name).toBe('CHANGES.md')
      expect(result.ignored).toEqual([
        { name: 'alpha.png', reason: 'thumbnail is Cover.png' },
        { name: 'notes.md', reason: 'README is CHANGES.md' },
        { name: 'zebra.png', reason: 'thumbnail is Cover.png' },
      ])
    }
  })

  it('still lets the preferred names win, in any order and any case', () => {
    const preferred = ['a.png', 'THUMBNAIL.PNG', 'a.md', 'ReadMe.md', 'model.scad']
    for (const order of [preferred, [...preferred].reverse()]) {
      const { files } = classifyFiles(order.map((name) => file(name)))
      expect(files?.thumbnail?.name).toBe('THUMBNAIL.PNG')
      expect(files?.readme?.name).toBe('ReadMe.md')
    }
  })

  it('picks the same model.json among several, whatever the order', () => {
    const set = () => [
      file('model.scad'),
      file('Model.json', '{"name": "Capital"}'),
      file('model.json', '{"name": "Lower"}'),
    ]
    const forward = classifyFiles(set())
    const backward = classifyFiles(set().reverse())

    for (const result of [forward, backward]) {
      // Code-unit order: `M` before `m`.
      expect(result.files?.meta?.name).toBe('Model.json')
      expect(result.ignored).toEqual([{ name: 'model.json', reason: 'metadata is Model.json' }])
    }
  })

  it('names the model.json it used by its path when two share a name', () => {
    const set = () => [
      inFolder('widget', 'model.scad'),
      inFolder('widget', 'model.json'),
      inFolder('widget/old', 'model.json'),
    ]
    for (const order of [set(), set().reverse()]) {
      const { files, ignored } = classifyFiles(order, 'widget')
      expect(files?.meta?.webkitRelativePath).toBe('widget/model.json')
      expect(ignored).toEqual([{ name: 'model.json', reason: 'metadata is widget/model.json' }])
    }
  })

  it('picks the same source among several .scad files with no model.scad', () => {
    const scads = ['b.scad', 'a.scad', 'c.scad']
    expect(classifyFiles(scads.map((name) => file(name))).files?.scad.name).toBe('a.scad')
    expect(
      classifyFiles([...scads].reverse().map((name) => file(name))).files?.scad.name,
    ).toBe('a.scad')
  })
})

describe('thumbnailProblem', () => {
  it('takes a PNG up to 2 MiB, and names what is wrong otherwise', () => {
    const png = (size: number) => new File([new Uint8Array(size)], 'cover.png')
    expect(thumbnailProblem(png(2 * 1024 * 1024))).toBeNull()
    expect(thumbnailProblem(png(2 * 1024 * 1024 + 1))).toBe('The thumbnail must be 2 MiB or smaller.')
    expect(thumbnailProblem(file('cover.jpg'))).toBe('The thumbnail must be a PNG.')
  })
})

describe('folderOf', () => {
  it('names the directory a directory picker read', () => {
    expect(folderOf([inFolder('widget', 'model.scad')])).toBe('widget')
  })

  it('is undefined for files chosen one by one', () => {
    expect(folderOf([file('model.scad')])).toBeUndefined()
    expect(folderOf([])).toBeUndefined()
  })
})

describe('uploadFilename', () => {
  const scad = file('model.scad')

  it('names the source after its directory, as the image seed takes the slug', () => {
    expect(uploadFilename({ scad, folder: 'name-keychain' }, 'Name Keychain')).toBe(
      'name-keychain.scad',
    )
  })

  it('falls back to the model.json name for a bare model.scad', () => {
    expect(uploadFilename({ scad }, 'Name Keychain')).toBe('Name Keychain.scad')
  })

  it('keeps any other filename as it is', () => {
    expect(uploadFilename({ scad: file('Vase Mode.scad') }, 'Ignored')).toBe('Vase Mode.scad')
    expect(uploadFilename({ scad })).toBe('model.scad')
  })
})

describe('readMetaName', () => {
  it('reads the name out of a model.json', async () => {
    expect(await readMetaName(file('model.json', '{"name": "Widget"}'))).toBe('Widget')
  })

  it('is undefined for a missing, nameless or unreadable model.json', async () => {
    expect(await readMetaName(undefined)).toBeUndefined()
    expect(await readMetaName(file('model.json', '{"tags": []}'))).toBeUndefined()
    expect(await readMetaName(file('model.json', '{not json'))).toBeUndefined()
  })
})

describe('droppedFiles', () => {
  function directoryEntry(name: string, files: File[]) {
    const batches = [files.map((f) => ({ isFile: true, isDirectory: false, file: (ok: (f: File) => void) => ok(f) })), []]
    return {
      name,
      isFile: false,
      isDirectory: true,
      createReader: () => ({
        readEntries: (ok: (entries: unknown[]) => void) => ok(batches.shift() ?? []),
      }),
    }
  }

  it('reads a single dropped directory, one level deep', async () => {
    const scad = file('model.scad')
    const meta = file('model.json')
    const transfer = {
      items: [{ webkitGetAsEntry: () => directoryEntry('widget', [scad, meta]) }],
      files: [],
    } as unknown as DataTransfer

    expect(await droppedFiles(transfer)).toEqual({ files: [scad, meta], folder: 'widget' })
  })

  it('takes a drop of loose files as it is', async () => {
    const scad = file('model.scad')
    const transfer = {
      items: [{ webkitGetAsEntry: () => ({ isFile: true, isDirectory: false }) }],
      files: [scad],
    } as unknown as DataTransfer

    expect(await droppedFiles(transfer)).toEqual({ files: [scad] })
  })
})
