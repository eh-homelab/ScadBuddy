import Lightbox from 'yet-another-react-lightbox'
import Captions from 'yet-another-react-lightbox/plugins/captions'
import Video from 'yet-another-react-lightbox/plugins/video'
import Zoom from 'yet-another-react-lightbox/plugins/zoom'
import 'yet-another-react-lightbox/plugins/captions.css'
import 'yet-another-react-lightbox/styles.css'
import { isEmbedded } from '../../lib/embed'
import { toLightboxSlides, type Slide } from './slides'

const PLUGINS = [Captions, Video, Zoom]

/** The lightbox library itself, loaded with this chunk the first time media opens. */
export default function LightboxView({
  slides,
  index,
  onClose,
}: {
  slides: Slide[]
  index: number
  onClose: () => void
}) {
  return (
    <Lightbox
      open
      index={index}
      close={onClose}
      slides={toLightboxSlides(slides, isEmbedded())}
      plugins={PLUGINS}
      carousel={{ finite: true }}
      captions={{ descriptionTextAlign: 'center' }}
      controller={{ closeOnBackdropClick: true }}
    />
  )
}
