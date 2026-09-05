// theme.js - Design tokens. Warm, bright and playful on a sunlit cream canvas.
//
// Solid colour throughout - no gradients anywhere in this app.
//
// The categorical palette is VALIDATED, not hand-picked. This exact ordering
// clears every gate of the dataviz colour check on the #FFFBF5 surface
// (lightness band, chroma floor, adjacent-pair CVD separation, normal-vision
// floor, contrast). Four earlier attempts failed - amber sitting next to
// green is the recurring trap, at CVD dE as low as 1.3, which no amount of
// looking at it would ever catch. If you reorder or swap a hue, re-run:
//   node scripts/validate_palette.js "<hexes>" --mode light --surface "#FFFBF5"
//
// The bright hues are for FILLS and large marks. Anything rendered as small
// text gets the darkened `*_TEXT` twin, because a cheerful yellow at 2:1
// against cream is decoration, not information.

export const CHROME = {
  pagePlane: '#FFFBF5',     // warm cream - sunlight, not office white
  surface: '#FFFFFF',
  surfaceRaised: '#FFF4E6', // warm tint for headers, hover, nested blocks
  surfaceActive: '#FFE8CC', // selected row, pressed control
  surfaceCool: '#F3F0FF',   // secondary wash, for variety between panels

  ink: '#2B2118',           // warm near-black, softer than pure black
  inkSecondary: '#4F4034',
  inkMuted: '#6B5B4D',
  inkDim: '#7E6D5B',

  border: '#F0E4D4',
  borderStrong: '#DFCDB6',
  gridline: '#F5EDE2',
  baseline: '#E4D5C2',

  // PRIMARY is the sunshine orange - this is a bright product, so the main
  // action gets the brightest thing on the page.
  primary: '#F76707',
  primaryInk: '#FFFFFF',    // large/bold button text only
  primaryDeep: '#C2410C',   // darkened, for primary-coloured TEXT

  accent: '#1C7ED6',        // secondary action / links
  accentInk: '#FFFFFF',
  accentText: '#1864AB',
  accentSoft: '#E7F5FF',
}

// Categorical series, FIXED order. See the header note before changing it.
export const SERIES = ['#F76707', '#1C7ED6', '#D6AF00', '#7048E8', '#2F9E44', '#E64980']

// Darkened twins of the above, for when a series colour has to be small text.
export const SERIES_TEXT = ['#C2410C', '#1864AB', '#836A00', '#5F3DC4', '#2B7A38', '#C2255C']

export const MARKS = {
  primary: '#F76707',
  secondary: '#1C7ED6',
  tertiary: '#2F9E44',
  track: '#FFF0DC',
}

// Sequential ramp for the heatmap: one hue, light -> dark. Magnitude is
// ordered, so it gets a ramp rather than categorical colours.
export const HEAT_STEPS = ['#FFF8E7', '#FFE8A3', '#FFC078', '#FF922B', '#E8590C', '#B8320A']

// Severity: `color` is the bright hue for marks and fills, `textColor` the
// darkened twin used wherever severity is rendered as words.
export const SEVERITY_META = {
  EMERGENCY: { glyph: '◆', rank: 1, color: '#E03131', textColor: '#A61E1E' },
  FATAL:     { glyph: '◆', rank: 1, color: '#E03131', textColor: '#A61E1E' },
  ALERT:     { glyph: '◆', rank: 1, color: '#E03131', textColor: '#A61E1E' },
  CRITICAL:  { glyph: '●', rank: 2, color: '#E64980', textColor: '#C2255C' },
  ERROR:     { glyph: '▲', rank: 3, color: '#F76707', textColor: '#C2410C' },
  WARNING:   { glyph: '▲', rank: 4, color: '#D6AF00', textColor: '#836A00' },
  NOTICE:    { glyph: '■', rank: 5, color: '#1C7ED6', textColor: '#1864AB' },
  INFO:      { glyph: '●', rank: 6, color: '#2F9E44', textColor: '#2B7A38' },
  DEBUG:     { glyph: '·', rank: 7, color: '#94826F', textColor: '#6B5B4D' },
}

export function severityMeta(severity) {
  return (
    SEVERITY_META[(severity || '').toUpperCase()] ||
    { glyph: '·', rank: 6, color: CHROME.inkMuted, textColor: CHROME.inkMuted }
  )
}

export const STATUS = {
  good: '#2F9E44',
  warning: '#D6AF00',
  danger: '#E03131',
  neutral: '#94826F',
}

// Mirrors analytics_pipeline/config.py's ALERT_LEVELS. The server decides
// which score falls in which band (rows arrive tagged with `alert_level`);
// this is only how each band is presented.
//
// Three values per level because one colour cannot do both jobs: `color` is
// the bright fill, `ink` the text to put on that fill, `textColor` the
// darkened twin for bare text.
export const ALERT_LEVEL_META = {
  critical: { label: 'Critical', priority: 'P1', glyph: '●',
              color: '#E64980', ink: '#FFFFFF', textColor: '#C2255C' },
  error:    { label: 'Error',    priority: 'P2', glyph: '▲',
              color: '#F76707', ink: '#FFFFFF', textColor: '#C2410C' },
  warning:  { label: 'Warning',  priority: 'P3', glyph: '▲',
              color: '#D6AF00', ink: '#2B2118', textColor: '#836A00' },
}

export function alertLevelMeta(level) {
  return ALERT_LEVEL_META[level] || null
}

export const ALERT_MAX_SCORE = 2
export const ERROR_MAX_SCORE = 3
export const WARNING_MAX_SCORE = 4

// Stable colour for an arbitrary label. Used to give each panel and each bar
// its own hue without threading a colour prop through every call site - the
// same title always lands on the same step, so a panel does not change colour
// when the grid is reordered.
export function hueFor(key, palette = SERIES) {
  const s = String(key || '')
  let h = 0
  for (let i = 0; i < s.length; i += 1) h = (h * 31 + s.charCodeAt(i)) >>> 0
  return palette[h % palette.length]
}

/* ---------------------------------------------------------------------------
 * GLASS - translucent surfaces over a blurred backdrop.
 *
 * Real glassmorphism, built WITHOUT a single gradient: the softness comes from
 * `backdrop-filter: blur()` acting on solid-colour shapes behind the page (see
 * AuroraBackdrop), not from a `linear-gradient` painted into the panel. That
 * matters because the usual glass recipe reaches for a diagonal white sheen,
 * and this app has none anywhere.
 *
 * Two things glass gets wrong by default and this avoids:
 *
 *  - Text over a translucent surface has no guaranteed contrast, because the
 *    contrast depends on whatever happens to be behind it. `surface` here is
 *    82% opaque over a light plane, which keeps the effective background above
 *    #F4EFE8 no matter what colour drifts under it - so the measured ink
 *    contrasts still hold.
 *
 *  - `backdrop-filter` is expensive per element. It is applied to panels and
 *    the two chrome bars only, never to table rows or list items, where a few
 *    hundred blurred layers would drop the frame rate through the floor.
 *
 * Corners are square everywhere by design - the glass reads as sheet material
 * rather than as a bubble.
 */
export const GLASS = {
  // Panels and cards.
  surface: 'rgba(255, 255, 255, 0.80)',
  surfaceRaised: 'rgba(255, 244, 230, 0.76)',
  // Sidebar and page header - slightly denser, since they sit over content.
  chrome: 'rgba(255, 251, 245, 0.86)',
  // Hairline that gives the sheet an edge. Light on top, so it reads as lit.
  border: 'rgba(255, 255, 255, 0.65)',
  borderOuter: 'rgba(43, 33, 24, 0.10)',
  blur: 'blur(18px) saturate(150%)',
  blurStrong: 'blur(26px) saturate(160%)',
  // A single soft drop shadow, no inset sheen.
  shadow: '0 2px 14px rgba(43, 33, 24, 0.07)',
  shadowRaised: '0 6px 26px rgba(43, 33, 24, 0.10)',
}

/* The style object every panel-like surface uses. One definition so a change
 * to the material is a change in one place, and so no panel drifts into being
 * subtly different glass from its neighbour. */
export function glassPanel({ raised = false } = {}) {
  return {
    background: raised ? GLASS.surfaceRaised : GLASS.surface,
    backdropFilter: GLASS.blur,
    WebkitBackdropFilter: GLASS.blur,
    border: `1px solid ${GLASS.borderOuter}`,
    borderTopColor: GLASS.border,
    boxShadow: raised ? GLASS.shadowRaised : GLASS.shadow,
  }
}
