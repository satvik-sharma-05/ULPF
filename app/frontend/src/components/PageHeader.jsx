import { CHROME, GLASS } from './analytics/theme.js'

// Shared page header so every page has identical title/subtitle/action
// geometry - the kind of consistency that reads as "designed" rather than
// "assembled" when four pages are flipped through quickly.
export default function PageHeader({ title, subtitle, actions }) {
  return (
    <header
      className="px-6 py-4 flex items-start justify-between gap-4 flex-wrap shrink-0 sticky top-0"
      style={{
        // Denser glass than a panel: this bar sits over scrolling content, and
        // at panel opacity the text underneath would read through it.
        background: GLASS.chrome,
        backdropFilter: GLASS.blurStrong,
        WebkitBackdropFilter: GLASS.blurStrong,
        borderBottom: `1px solid ${GLASS.borderOuter}`,
        zIndex: 20,
      }}
    >
      <div>
        <h1 className="text-lg font-semibold tracking-tight" style={{ color: CHROME.ink }}>
          {title}
        </h1>
        {subtitle && (
          <p className="text-xs mt-0.5" style={{ color: CHROME.inkMuted }}>{subtitle}</p>
        )}
      </div>
      {actions && <div className="flex items-center gap-2 flex-wrap">{actions}</div>}
    </header>
  )
}
