import type { FontFamily, Param, ParamValue } from '../../api/types'
import { BooleanWidget } from './BooleanWidget'
import { ColorWidget } from './ColorWidget'
import { FontWidget } from './FontWidget'
import { NumberWidget } from './NumberWidget'
import { SelectWidget } from './SelectWidget'
import { SliderWidget } from './SliderWidget'
import { TextWidget } from './TextWidget'

export interface ParamWidgetProps {
  param: Param
  value: ParamValue
  fonts: FontFamily[]
  /** Seeds the font picker's preview: the text this model will actually set. */
  sampleText?: string
  /** 1-based extruder index for `color` params (spec §7). */
  extruder?: number
  onChange: (next: ParamValue) => void
}

export function ParamWidget({
  param,
  value,
  fonts,
  sampleText,
  extruder,
  onChange,
}: ParamWidgetProps) {
  switch (param.type) {
    case 'slider':
      return <SliderWidget param={param} value={Number(value)} onChange={onChange} />
    case 'number':
    case 'integer':
      return <NumberWidget param={param} value={Number(value)} onChange={onChange} />
    case 'boolean':
      return <BooleanWidget param={param} value={Boolean(value)} onChange={onChange} />
    case 'select':
      return <SelectWidget param={param} value={value} onChange={onChange} />
    case 'color':
      return (
        <ColorWidget
          param={param}
          value={String(value)}
          extruder={extruder}
          onChange={onChange}
        />
      )
    case 'font':
      return (
        <FontWidget
          param={param}
          value={String(value)}
          fonts={fonts}
          sampleText={sampleText}
          onChange={onChange}
        />
      )
    case 'string':
      return <TextWidget param={param} value={String(value)} onChange={onChange} />
  }
}
