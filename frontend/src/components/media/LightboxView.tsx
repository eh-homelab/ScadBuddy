import Lightbox from 'yet-another-react-lightbox'
import Captions from 'yet-another-react-lightbox/plugins/captions'
import Video from 'yet-another-react-lightbox/plugins/video'
import Zoom from 'yet-another-react-lightbox/plugins/zoom'
import 'yet-another-react-lightbox/plugins/captions.css'
import 'yet-another-react-lightbox/styles.css'
import { Link } from 'react-router'
import { isEmbedded } from '../../lib/embed'
import { toLightboxSlides, type Slide, type SlideLink } from './slides'

const PLUGINS = [Captions, Video, Zoom]

/** The lightbox library itself, loaded with this chunk the first time media opens. */
export default function LightboxView({
  slides,
  index,
  onClose,
  link,
}: {
  slides: Slide[]
  index: number
  onClose: () => void
  link?: SlideLink
}) {
  // A link in the toolbar goes on from the media without closing it first (the
  // catalogue's "Open template"); the plugins add their buttons before 'close'.
  const buttons = link
    ? [
        <Link
          key="link"
          to={link.to}
          className="yarl__button self-center rounded-[4px] px-3 text-[13px] font-medium"
        >
          {link.label}
        </Link>,
        'close',
      ]
    : ['close']
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
      toolbar={{ buttons }}
    />
  )
}
