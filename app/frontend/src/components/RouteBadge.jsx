import { CHROME } from './analytics/theme.js'

// Route as an inverted monospace tag. With a monochrome palette the strategy
// name itself is the encoding - there is no per-strategy hue to lean on.
export default function RouteBadge({ route }) {
  if (!route) return null
  return (
    <span
      className="inline-flex items-center gap-1.5 px-1.5 py-0.5 text-[9px] font-mono uppercase tracking-wide"
      style={{ background: CHROME.primary, color: CHROME.primaryInk }}
      title={route.reason}
    >
      {route.strategy}
      {typeof route.confidence === 'number' && (
        <span style={{ opacity: 0.7 }}>{Math.round(route.confidence * 100)}%</span>
      )}
    </span>
  )
}
