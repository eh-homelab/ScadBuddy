import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { NEW_TAB } from '../../../lib/embed'
import { safeHttpUrl } from '../../../lib/safeUrl'
import { FieldRow } from '../controls'
import { docOf } from '../fields'
import { Advanced, RuntimeRows, SavedSection } from '../formParts'
import type { AreaId } from '../registry'
import { useBackendSettings } from '../useBackendSettings'
import { ACTIVITY_REPAIR, ACTIVITY_TTL, CREDENTIAL_RETENTION, fieldsOf } from '../values'

export const id = 'administration'
export const area: AreaId = 'system'
export const title = 'Administration'
export const order = 20

/**
 * Who the activity bell follows, how long activity and ended service credentials are kept
 * (#8539), and the Temporal UI (#668).
 */
export function Component() {
  const form = useBackendSettings(id, (s) => fieldsOf(id, s))
  const { value, setField, errors } = form
  const temporalUi = safeHttpUrl(form.settings.temporal_ui_url)
  return (
    <SavedSection form={form} id={id} title={title}>
      <FieldRow
        id="activity-follow"
        {...docOf(form.settings, 'activity_follow')}
        error={errors.activity_follow}
      >
        <Input
          id="activity-follow"
          type="text"
          placeholder="browser"
          value={value('activity_follow')}
          onChange={(event) => setField('activity_follow', event.target.value)}
        />
      </FieldRow>
      <Advanced
        label="Advanced: activity retention"
        invalid={Boolean(errors.activity_ttl_seconds || errors.activity_repair_window_seconds)}
      >
        <FieldRow
          id="activity-ttl"
          {...docOf(form.settings, 'activity_ttl_seconds')}
          error={errors.activity_ttl_seconds}
        >
          <Input
            id="activity-ttl"
            type="number"
            min={ACTIVITY_TTL.min}
            max={ACTIVITY_TTL.max}
            step={1}
            inputMode="numeric"
            placeholder={String(ACTIVITY_TTL.default)}
            value={value('activity_ttl_seconds')}
            onChange={(event) => setField('activity_ttl_seconds', event.target.value)}
            className="sb-num"
          />
        </FieldRow>
        <FieldRow
          id="activity-repair"
          {...docOf(form.settings, 'activity_repair_window_seconds')}
          error={errors.activity_repair_window_seconds}
        >
          <Input
            id="activity-repair"
            type="number"
            min={ACTIVITY_REPAIR.min}
            max={ACTIVITY_REPAIR.max}
            step={1}
            inputMode="numeric"
            placeholder={String(ACTIVITY_REPAIR.default)}
            value={value('activity_repair_window_seconds')}
            onChange={(event) => setField('activity_repair_window_seconds', event.target.value)}
            className="sb-num"
          />
        </FieldRow>
      </Advanced>
      <Advanced
        label="Advanced: service credential retention"
        invalid={Boolean(errors.service_credential_retention_days)}
      >
        <FieldRow
          id="credential-retention"
          {...docOf(form.settings, 'service_credential_retention_days')}
          error={errors.service_credential_retention_days}
        >
          <Input
            id="credential-retention"
            type="number"
            min={CREDENTIAL_RETENTION.min}
            max={CREDENTIAL_RETENTION.max}
            step={1}
            inputMode="numeric"
            placeholder="Default"
            value={value('service_credential_retention_days')}
            onChange={(event) => setField('service_credential_retention_days', event.target.value)}
            className="sb-num"
          />
        </FieldRow>
      </Advanced>
      <RuntimeRows form={form} section="administration" />
      {temporalUi && (
        // Not ScadBuddy's page: a new tab, which also escapes Bambuddy's sandbox.
        <Button asChild variant="link" className="h-auto px-0">
          <a href={temporalUi} {...NEW_TAB}>
            Temporal UI ↗
          </a>
        </Button>
      )}
    </SavedSection>
  )
}
