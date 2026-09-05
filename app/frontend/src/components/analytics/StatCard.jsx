import { CHROME, GLASS, glassPanel, hueFor } from './theme.js'
import { formatCompact } from './format.js'
import { useCountUp } from '../ui/motion.jsx'

/* Stat tile with a count-up. The animation is only applied to genuine numbers;
 * a pre-formatted string (a date range, an em dash) is rendered as-is rather
 * than being coerced into a number and animated to NaN.
 *
 * The accent colour paints a rail down the left edge and tints the number. A
 * caller can pass one explicitly (severity tiles do, so the colour actually
 * means something); otherwise it is derived from the label, which keeps a row
 * of tiles varied without any of them claiming a meaning they don't have.
 */
export default function StatCard({ label, value, sublabel, compact = true, emphasis = false, accent }) {
  const numeric = typeof value === 'number' && Number.isFinite(value)
  const animated = useCountUp(numeric ? value : 0)
  const shown = numeric
    ? (compact ? formatCompact(Math.round(animated)) : Math.round(animated).toLocaleString())
    : (value ?? '—')

  const rail = accent || hueFor(label)

  // A long string value gets a smaller size. At text-2xl a date range wrapped
  // onto three lines and broke mid-date - "2026-08-" / "28" - which is not a
  // legible number and not a legible date either. Numbers are never affected;
  // they are short by construction and are the reason the tile is big.
  // A node counts as long as well as a long string. The date-range tile passes
  // a two-line element, and checking only for strings meant it fell through to
  // the headline size and overflowed its card into the next one.
  const longText = !numeric && (typeof shown !== 'string' || shown.length > 11)
  const valueClass = longText
    ? 'text-sm font-bold leading-snug'
    : 'text-2xl font-extrabold leading-none'

  return (
    <div
      className="pl-4 pr-4 py-3 flex flex-col gap-1 transition-transform hover:-translate-y-0.5"
      style={{
        ...glassPanel({ raised: emphasis }),
        // The accent survives on the tiles as a left rail, unlike the top rule
        // that was removed from panels. It is doing work here: severity tiles
        // pass an explicit colour, so the rail carries the level rather than
        // just decorating the card.
        borderLeft: `4px solid ${rail}`,
      }}
    >
      <span className="text-[10px] uppercase tracking-wide font-semibold" style={{ color: CHROME.inkMuted }}>
        {label}
      </span>
      <span
        className={valueClass}
        style={{ color: CHROME.ink, fontVariantNumeric: 'proportional-nums' }}
      >
        {shown}
      </span>
      {sublabel && (
        <span className="text-[10px]" style={{ color: CHROME.inkMuted }}>{sublabel}</span>
      )}
    </div>
  )
}
