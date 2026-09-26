import { describe, expect, it } from 'vitest'
import {
  classifyFiles,
  droppedFiles,
  folderOf,
  readMetaName,
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
    expect(ignored).toEqual(['verify.sh'])
  })

  it('prefers the bundled names when there is more than one candidate', () => {
    const helper = file('helper.scad')
    const scad = file('model.scad')
    const photo = file('photo.png')
    const thumbnail = file('thumbnail.png')

    const { files, ignored } = classifyFiles([helper, photo, scad, thumbnail])

    expect(files?.scad).toBe(scad)
    expect(files?.thumbnail).toBe(thumbnail)
    expect(ignored).toEqual(['helper.scad', 'photo.png'])
  })

  it('takes a lone .scad on its own', () => {
    const scad = file('Vase Mode.scad')
    expect(classifyFiles([scad]).files).toEqual({ scad, folder: undefined })
  })

  it('has nothing to upload without a source', () => {
    const { files, ignored } = classifyFiles([file('thumbnail.png'), file('model.stl')])
    expect(files).toBeNull()
    expect(ignored).toEqual(['thumbnail.png', 'model.stl'])
  })

  it('does not take any .json as the metadata', () => {
    const { files, ignored } = classifyFiles([file('model.scad'), file('package.json')])
    expect(files?.meta).toBeUndefined()
    expect(ignored).toEqual(['package.json'])
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
