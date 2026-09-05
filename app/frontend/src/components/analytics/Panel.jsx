import { CHROME, GLASS, glassPanel } from './theme.js'

/* Shared panel chrome so every analytics card has identical header geometry.
 *
 * One sheet of glass: translucent, blurred over the backdrop, hairline border,
 * square corners. The material comes from glassPanel() rather than being
 * written out here, so every panel in the app is demonstrably the same glass -
 * the failure mode of this style is fifteen panels that are each very slightly
 * different, which reads as sloppy rather than as depth.
 *
 * `accent` is still accepted so existing call sites do not break, but it is
 * no longer painted: the coloured rule that used to sit across the top of each
 * card is gone.
 */
export default function Panel({ title, subtitle, action, children, className = '', bodyClass = 'p-4', accent }) {
  return (
    <section
      className={`overflow-hidden ${className}`}
      style={glassPanel()}
    >
      <header
        className="px-4 py-2.5 flex items-start justify-between gap-3"
        style={{ borderBottom: `1px solid ${GLASS.borderOuter}` }}
      >
        <div className="min-w-0">
          <h3 className="text-xs font-bold uppercase tracking-wide" style={{ color: CHROME.ink }}>{title}</h3>
          {subtitle && <p className="text-[10px] mt-0.5" style={{ color: CHROME.inkMuted }}>{subtitle}</p>}
        </div>
        {action && <div className="shrink-0">{action}</div>}
      </header>
      <div className={bodyClass}>{children}</div>
    </section>
  )
}
