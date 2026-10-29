import { USER_ONLY } from '../../../agent/dom'
import { api, ApiError } from '../../../api/client'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Switch } from '@/components/ui/switch'
import { ChoiceSelect } from '../../../components/settings/form'
import { formatBytes } from '../../../lib/format'
import { breadcrumbs } from '../../../lib/projects'
import { useAsync } from '../../../lib/useAsync'
import { useSettingsAgentField } from '../agentFields'
import { useBambuddyResources } from '../bambuddyResources'
import { FieldRow } from '../controls'
import { usePeerDraft } from '../dirty'
import { FieldBadge, RuntimeRows, SavedSection } from '../formParts'
import type { AreaId } from '../registry'
import { useBackendSettings } from '../useBackendSettings'
import { fieldsOf, ofLimit, storeChoice } from '../values'

export const id = 'projects'
export const area: AreaId = 'library'
export const title = 'Projects & files'
export const order = 10

/** Where sends are filed, the blob store (#426) and its S3 settings, the print library and the slicer. */
export function Component() {
  const { targets, projects: projectsState } = useBambuddyResources()
  const form = useBackendSettings(id, (s) => fieldsOf(id, s), {
    // The Blob store choice sends and compares what it shows: local until the chosen store is ready.
    effective: (name, raw, f) => (name === 'store_backend' ? storeChoice(f.settings, f.value).backend : raw),
    // Resetting the inbox the Bambuddy store needs takes the store back to local with it, as a
    // save that loses it does: the server refuses an unready Bambuddy store.
    alsoReset: (name, s) =>
      name === 'library_folder_id' && s.store_backend === 'bambuddy' && s.library_backend !== 'native'
        ? { store_backend: 'local' }
        : {},
  })
  const { settings, value, setField, errors, clearing, clearSecret } = form
  const store = storeChoice(settings, value)
  // Connection's unsaved Bambuddy URL, when Connection is mounted.
  const bambuddyUrlDraft = usePeerDraft('bambuddy_url') ?? ''
  const storeUsage = useAsync(() => api.getStoreUsage(), []).data

  const folders = targets.data?.folders ?? []
  const projects = projectsState.data?.projects ?? []
  const project = value('last_project_id')
  const projectPaths = breadcrumbs(projects)

  useSettingsAgentField('library_folder_id', {
    value: () => value('library_folder_id'),
    set: (next) => setField('library_folder_id', next),
    allowed: () => ['', ...folders.map((folder) => String(folder.id))],
    listed: () => folders.map((folder) => ({ value: String(folder.id), name: folder.name })),
  })

  return (
    <SavedSection form={form} id={id} title={title}>
      <FieldRow
        id="library-folder"
        label="Inbox folder, for sends without a project"
        help="Where a 3MF goes when it is sent without a project. A send to a project uses the project's own folder."
      >
        <ChoiceSelect
          id="library-folder"
          value={value('library_folder_id')}
          onValueChange={(next) => setField('library_folder_id', next)}
          choices={[
            { value: '', label: 'Library root' },
            ...folders.map((folder) => ({ value: String(folder.id), label: folder.name })),
          ]}
        />
      </FieldRow>

      <FieldRow
        id="default-project"
        label="Default project"
        help={
          projectsState.error
            ? `Could not list the projects: ${projectsState.error instanceof ApiError ? projectsState.error.detail : 'no answer'}`
            : 'Where a print, Arrange or Generate is filed when what it holds is not all in one project already (then it opens on that project). A model opens on the project it was last filed in. None set: no project.'
        }
      >
        <ChoiceSelect
          id="default-project"
          value={project}
          onValueChange={(next) => setField('last_project_id', next)}
          choices={[
            { value: '', label: 'No project' },
            ...(project && !projects.some((entry) => String(entry.id) === project)
              ? [{ value: project, label: `Project ${project}` }]
              : []),
            ...projects.map((entry) => ({ value: String(entry.id), label: projectPaths.get(entry.id) ?? entry.name })),
          ]}
        />
      </FieldRow>

      <FieldRow
        id="destination-sort"
        label="Sort projects and folders by"
        help="How the project and folder picker opens. It can be re-sorted there."
      >
        <ChoiceSelect
          id="destination-sort"
          value={value('destination_sort')}
          onValueChange={(next) => setField('destination_sort', next)}
          choices={[
            { value: 'touched', label: 'Last touched: printed or filed' },
            { value: 'modified', label: 'Modified in Bambuddy' },
            { value: 'name', label: 'Name' },
          ]}
        />
      </FieldRow>

      <FieldRow
        id="store-backend"
        label="Blob store"
        badge={<FieldBadge form={form} name="store_backend" />}
        error={errors.store_backend}
        help="Takes effect when ScadBuddy and its render workers restart."
      >
        <ChoiceSelect
          id="store-backend"
          value={store.backend}
          onValueChange={(next) => setField('store_backend', next)}
          choices={[
            { value: 'local', label: 'This server’s volume (one render worker)' },
            {
              value: 'bambuddy',
              label: 'Bambuddy library (any number of render workers)',
              disabled: !store.bambuddyReady,
            },
            {
              value: 's3',
              label: 'S3-compatible object store (any number of render workers)',
              disabled: !store.s3Ready,
            },
          ]}
        />
        {!store.bambuddyReady && (
          <p className="mt-1.5 text-xs text-muted-foreground" data-testid="store-backend-hint">
            {!store.savedBambuddyUrl && bambuddyUrlDraft !== ''
              ? 'The Bambuddy URL is not saved yet: save Connection to choose the Bambuddy library.'
              : 'The Bambuddy library needs a saved Bambuddy URL and an inbox folder.'}
          </p>
        )}
        {!store.s3Ready && (
          <p className="mt-1.5 text-xs text-muted-foreground" data-testid="store-s3-hint">
            The S3 store needs its endpoint, bucket and keys saved first.
          </p>
        )}
      </FieldRow>

      <FieldRow
        id="library-backend"
        label="Print library"
        badge={<FieldBadge form={form} name="library_backend" />}
        error={errors.library_backend}
        help="Where print files are kept. Takes effect when the ScadBuddy API restarts; its workers need no restart. Switching moves no files: ScadBuddy’s own library starts empty, and Bambuddy’s files stay in Bambuddy."
      >
        <ChoiceSelect
          id="library-backend"
          value={value('library_backend') === 'native' ? 'native' : 'bambuddy'}
          onValueChange={(next) => setField('library_backend', next)}
          choices={[
            { value: 'bambuddy', label: 'Bambuddy’s' },
            { value: 'native', label: 'ScadBuddy’s own', disabled: store.backend === 'local' },
          ]}
        />
        {store.backend === 'local' && (
          <p className="mt-1.5 text-xs text-muted-foreground" data-testid="library-backend-hint">
            ScadBuddy’s own library keeps its files in the blob store: choose the S3 or Bambuddy store first.
          </p>
        )}
      </FieldRow>

      <FieldRow
        id="slicer-backend"
        label="Slicer"
        badge={<FieldBadge form={form} name="slicer_backend" />}
        error={errors.slicer_backend}
        help="Who slices the print dialog’s plates and the prints it queues. Takes effect at once. ScadBuddy’s slicer runs the Bambu Studio CLI in the scadbuddy-slicer worker; Bambuddy still queues and prints."
      >
        <ChoiceSelect
          id="slicer-backend"
          value={value('slicer_backend') === 'native' ? 'native' : 'bambuddy'}
          onValueChange={(next) => setField('slicer_backend', next)}
          choices={[
            { value: 'bambuddy', label: 'Bambuddy’s slicer' },
            { value: 'native', label: 'ScadBuddy’s slicer (Bambu Studio CLI)' },
          ]}
        />
      </FieldRow>

      <div role="group" aria-labelledby="store-s3-heading" className="space-y-4">
        <p id="store-s3-heading" className="text-sm font-medium">
          S3 store
        </p>
        {(
          [
            ['store_s3_endpoint', 'Endpoint', 'The service’s URL, e.g. https://s3.us-east-1.amazonaws.com or a MinIO or Garage address.'],
            ['store_s3_region', 'Region', 'The region requests are signed for; most self-hosted services take any.'],
            ['store_s3_bucket', 'Bucket', 'An existing bucket. ScadBuddy does not create it.'],
            ['store_s3_prefix', 'Prefix', 'Every object goes under this key prefix in the bucket.'],
            ['store_s3_access_key_id', 'Access key id', undefined],
          ] as const
        ).map(([name, label, help]) => {
          const id = `setting-${name}`
          return (
            <FieldRow key={name} id={id} label={label} badge={<FieldBadge form={form} name={name} />} error={errors[name]} help={help}>
              <Input
                id={id}
                value={value(name)}
                autoComplete="off"
                onChange={(event) => setField(name, event.target.value)}
              />
            </FieldRow>
          )
        })}
        <FieldRow
          id="setting-store_s3_secret_access_key"
          label="Secret access key"
          badge={<FieldBadge form={form} name="store_s3_secret_access_key" />}
          error={errors.store_s3_secret_access_key}
          help="Sealed at rest, and never shown again once saved."
        >
          <div className="flex gap-2">
            <Input
              id="setting-store_s3_secret_access_key"
              type="password"
              value={value('store_s3_secret_access_key')}
              autoComplete="off"
              onChange={(event) => setField('store_s3_secret_access_key', event.target.value)}
              placeholder={
                clearing.includes('store_s3_secret_access_key')
                  ? 'Cleared when you save.'
                  : settings.has_store_s3_secret_access_key
                    ? 'A key is stored. Paste a new one to replace it.'
                    : 'Paste the key'
              }
              className="sb-num"
            />
            {settings.has_store_s3_secret_access_key && (
              <Button
                variant="outline"
                disabled={clearing.includes('store_s3_secret_access_key')}
                onClick={() => clearSecret('store_s3_secret_access_key')}
                {...USER_ONLY}
              >
                Remove key
              </Button>
            )}
          </div>
        </FieldRow>
        <FieldRow
          id="setting-store_s3_path_style"
          label="Path-style addressing"
          badge={<FieldBadge form={form} name="store_s3_path_style" />}
          error={errors.store_s3_path_style}
          help="The bucket in the path rather than the hostname. Most self-hosted services need it on."
        >
          <Switch
            id="setting-store_s3_path_style"
            checked={value('store_s3_path_style') === 'true'}
            onCheckedChange={(checked) => setField('store_s3_path_style', String(checked))}
          />
        </FieldRow>
      </div>

      {storeUsage && (
        <Card size="sm">
          <CardContent>
          <p className="text-sm font-medium">Blob store usage</p>
          <dl className="mt-2 grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-sm" data-testid="store-usage">
            <dt className="text-muted-foreground">Where</dt>
            <dd>
              {storeUsage.backend === 'bambuddy'
                ? 'Bambuddy library'
                : storeUsage.backend === 's3'
                  ? 'S3 object store'
                  : 'This server’s volume'}
            </dd>
            <dt className="text-muted-foreground">Files</dt>
            <dd className="sb-num">{ofLimit(String(storeUsage.count), storeUsage.max_count, String)}</dd>
            <dt className="text-muted-foreground">Size</dt>
            <dd className="sb-num">
              {ofLimit(formatBytes(storeUsage.bytes), storeUsage.max_total_bytes, formatBytes)}
            </dd>
          </dl>
          <p className="mt-2 text-xs text-muted-foreground">
            The Where row is the store this process uses; it moves to the Blob store choice above at its
            next restart, so the two can differ until then.
          </p>
          <p className="mt-1.5 text-xs text-muted-foreground">
            Rendered pieces, template snapshots, uploaded SVGs and PNGs, and downloaded fonts. What no job,
            output or preset uses is removed once unused for the sweep&rsquo;s grace period (a week by
            default). Past either limit, new files are refused.
          </p>
          </CardContent>
        </Card>
      )}
      <RuntimeRows form={form} section={id} />
    </SavedSection>
  )
}
