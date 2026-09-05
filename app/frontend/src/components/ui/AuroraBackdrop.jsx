import { SERIES } from '../analytics/theme.js'

/* The thing the glass is glass OVER.
 *
 * `backdrop-filter: blur()` blurs whatever is painted behind the element. Over
 * a flat cream page that is nothing, and every "glass" panel comes out looking
 * like a slightly grey solid rectangle - the effect only exists if there is
 * something worth blurring underneath.
 *
 * So: a fixed layer of large SOLID-COLOUR circles in the palette hues, at low
 * alpha. No gradients anywhere - the soft wash people associate with this look
 * is produced entirely by the blur on the panels above, plus one blur on this
 * layer itself. A radial-gradient blob would have been the obvious way to do
 * it and is exactly what this app does not use.
 *
 * It is decorative and inert: aria-hidden, pointer-events none, fixed so it
 * never scrolls or reflows, and z-index 0 so all content sits above it.
 */

// x / y as viewport percentages, size in vmax, and the palette step. Placed by
// hand rather than randomly so the composition is the same on every load and a
// screenshot is reproducible.
// Alphas are as high as the contrast budget allows, not as high as looks
// nice. Worst case is the orange blob at 0.32 showing through a panel at 0.80
// opacity, which lands the effective panel background at rgb(255,246,240) -
// still light enough that the ink tokens keep the contrast they were measured
// at. Raising these further starts eating into that.
const BLOBS = [
  { x: -8, y: -12, size: 46, color: SERIES[0], alpha: 0.32 },  // orange, top left
  { x: 62, y: -18, size: 52, color: SERIES[3], alpha: 0.26 },  // violet, top right
  { x: 78, y: 46, size: 40, color: SERIES[1], alpha: 0.24 },   // blue, mid right
  { x: 10, y: 58, size: 44, color: SERIES[4], alpha: 0.22 },   // green, lower left
  { x: 40, y: 84, size: 36, color: SERIES[5], alpha: 0.22 },   // pink, bottom
  { x: 30, y: 20, size: 30, color: SERIES[2], alpha: 0.22 },   // gold, centre
]

function rgba(hex, a) {
  const n = parseInt(hex.slice(1), 16)
  return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`
}

export default function AuroraBackdrop() {
  return (
    <div
      aria-hidden="true"
      className="fixed inset-0 pointer-events-none overflow-hidden"
      style={{ zIndex: 0 }}
    >
      {BLOBS.map((b, i) => (
        <div
          key={i}
          className="absolute"
          style={{
            left: `${b.x}%`,
            top: `${b.y}%`,
            width: `${b.size}vmax`,
            height: `${b.size}vmax`,
            background: rgba(b.color, b.alpha),
            // The one place a circle survives in a square-cornered design:
            // this is a shape, not a panel corner.
            borderRadius: '50%',
            // Blurred here as well as by the glass above, so the edges of the
            // circles never read as circles.
            filter: 'blur(90px)',
          }}
        />
      ))}
    </div>
  )
}
