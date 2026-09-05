import { useCallback, useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { CHROME } from '../analytics/theme.js'

/* DecryptText — text that resolves out of scrambled glyphs, character by
 * character, with jitter. Ported from the Motiq component (MIT) to this
 * project's stack, which is React + JSX + Vite rather than Next.js +
 * TypeScript + shadcn, and to its own theme tokens rather than Motiq's.
 *
 * Two substantive changes beyond the port:
 *  - No `clsx`/`tailwind-merge` dependency. This app ships to an airgapped VM,
 *    and two packages to concatenate class strings is not worth the supply
 *    chain; the component takes a plain `className` instead.
 *  - Colours come from our CHROME tokens, so the effect matches the rest of
 *    the console instead of introducing a second palette.
 *
 * The accessibility design of the original is kept intact, because it is the
 * good part: the real string is always in the DOM for screen readers and for
 * a no-JS/SSR render, the animated glyph layer is aria-hidden, and
 * prefers-reduced-motion renders the resolved text with no animation at all.
 * One rAF loop per instance writes textContent only - no layout writes.
 */

const POOL_DISPLAY = '#%&@$?!*+=/{}[]<>~^'
const POOL_TERMINAL = 'abcdef0123456789$#%&*+=/|_~'
const HOVER_COOLDOWN = 1500
const CYCLE_SPREAD = 35
const FLASH_MS = 420

// mulberry32 - deterministic, so the jitter is stable across renders and a
// server/client pair can never disagree. Math.random at module scope would
// break that.
function makeRng(seed) {
  let a = seed >>> 0
  return () => {
    a = (a + 0x6d2b79f5) | 0
    let t = Math.imul(a ^ (a >>> 15), 1 | a)
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296
  }
}

function useReducedMotion() {
  const [reduced, setReduced] = useState(
    () => typeof window !== 'undefined' && window.matchMedia('(prefers-reduced-motion: reduce)').matches,
  )
  useEffect(() => {
    const mq = window.matchMedia('(prefers-reduced-motion: reduce)')
    setReduced(mq.matches)
    const onChange = (e) => setReduced(e.matches)
    mq.addEventListener('change', onChange)
    return () => mq.removeEventListener('change', onChange)
  }, [])
  return reduced
}

// Pauses the rAF loop when the element scrolls away or the tab is hidden -
// a decorative animation should not burn a core in a background tab.
function useVisibilityPause(ref, threshold = 0.12) {
  const [onScreen, setOnScreen] = useState(true)
  const [tabVisible, setTabVisible] = useState(true)

  useEffect(() => {
    const el = ref.current
    if (!el || typeof IntersectionObserver === 'undefined') return undefined
    const io = new IntersectionObserver(
      (entries) => setOnScreen(entries.some((e) => e.isIntersecting)),
      { threshold },
    )
    io.observe(el)
    return () => io.disconnect()
  }, [ref, threshold])

  useEffect(() => {
    const onVis = () => setTabVisible(document.visibilityState !== 'hidden')
    onVis()
    document.addEventListener('visibilitychange', onVis)
    return () => document.removeEventListener('visibilitychange', onVis)
  }, [])

  return onScreen && tabVisible
}

export default function DecryptText({
  text,
  glyphs,
  speed = 45,
  stagger = 55,
  startDelay = 350,
  jitter = 120,
  trigger = 'inview',
  variant = 'display',
  loop = 7000,
  retriggerOnHover = true,
  seed = 1,
  as: Tag = 'p',
  // Optional per-WORD colours, e.g. ['#0A0A0A', null, '#D9480F'] - a null
  // leaves that word on the default ink. Applied per word rather than per
  // character so the phrase still reads as words, not confetti.
  wordColors,
  // Optional per-WORD style objects (fontFamily, fontStyle, ...), merged onto
  // that word's characters. Same indexing as wordColors.
  wordStyles,
  // Style applied to the word's WRAPPER rather than its characters - the
  // place to reserve a fixed width when a word's typeface changes, so the
  // words after it don't shift.
  wordWrapStyles,
  // Fired when a scramble run BEGINS. The point of exposing this is that a
  // caller changing a word's typeface can do it here: swapping a font changes
  // the word's width, and doing that while the glyphs are already boiling
  // hides the reflow completely. Changing it at rest makes the headline jump.
  onRunStart,
  reducedMotion,
  onDecrypted,
  className = '',
  style,
  ...rest
}) {
  const rootRef = useRef(null)
  const charRefs = useRef([])
  // One entry per WORD wrapper - see freezeWidths().
  const wordRefs = useRef([])
  // Settled width per word, captured at the end of each run - see
  // freezeWidths() for why a live measurement is not trusted.
  const settledWidths = useRef([])
  // Gates the first run - see the seeding effect below.
  const [fontsReady, setFontsReady] = useState(false)
  const rafRef = useRef(null)
  const timerRef = useRef(null)
  const lastStartRef = useRef(-Infinity)
  const playedRef = useRef(false)
  const runRef = useRef(0)
  const onDecryptedRef = useRef(onDecrypted)
  onDecryptedRef.current = onDecrypted
  const onRunStartRef = useRef(onRunStart)
  onRunStartRef.current = onRunStart

  const uid = useId().replace(/[^a-zA-Z0-9]/g, '')
  const scope = `dt-${uid}`

  const systemReduced = useReducedMotion()
  const reduce = reducedMotion ?? systemReduced
  const visible = useVisibilityPause(rootRef)

  const pool = glyphs && glyphs.length > 0
    ? glyphs
    : variant === 'terminal' ? POOL_TERMINAL : POOL_DISPLAY

  // Split per word so the line still wraps on word boundaries rather than
  // mid-token once every character is its own span.
  const words = useMemo(() => {
    const out = []
    let i = 0
    for (const word of String(text).split(' ')) {
      const item = []
      for (const ch of Array.from(word)) {
        item.push({ i, ch })
        i += 1
      }
      out.push(item)
    }
    return out
  }, [text])

  const stop = useCallback(() => {
    if (rafRef.current != null) cancelAnimationFrame(rafRef.current)
    rafRef.current = null
    if (timerRef.current) clearTimeout(timerRef.current)
    timerRef.current = null
  }, [])

  /* Freezes each WORD at the width it has when settled.
   *
   * The scramble swaps each letter for a random symbol, and those symbols are
   * not the same width as the letters they stand in for. On a single-line
   * headline that only jitters; as soon as the headline wraps, the changing
   * total width re-solves the line breaks and the whole block gains or loses
   * a line mid-animation - a 43px jump, ten times a minute. Pinning each cell
   * makes the scramble typographically identical in width to the settled text,
   * so no reflow is possible while it runs.
   *
   * The word wrapper, not the individual characters. Pinning each character
   * looked more precise and was measurably worse: a character span already
   * carries the letter-spacing that follows it, and turning it into an
   * inline-block makes the parent apply that spacing a second time - the
   * glyph layer measured 170.3px scrambling against 162.8px settled, so the
   * "fix" reintroduced the reflow it was meant to remove. The wrapper is
   * already a box in the layout, so setting its width changes nothing except
   * making it non-negotiable.
   *
   * Measured at run start, while the words still show their real letters.
   */
  const freezeWidths = useCallback(() => {
    wordRefs.current.forEach((el, w) => {
      // A word whose wrapper already has an explicit width (a caller pinning
      // a word whose typeface changes) is left alone - it cannot reflow, and
      // clearing that width on release would drop the caller's style.
      if (!el || el.style.width) return
      // Prefer the width recorded the last time this word was verifiably
      // settled. Measuring live here proved unreliable - the value came back
      // as the scrambled width even after restoring the glyphs first, which
      // baked the reflow in rather than removing it. A width captured at a
      // known-settled moment cannot be wrong in that way.
      const known = settledWidths.current[w]
      const width = known ?? el.getBoundingClientRect().width
      el.style.width = `${width}px`
      el.dataset.wfrozen = '1'
    })
  }, [])

  const releaseWidths = useCallback((record = false) => {
    wordRefs.current.forEach((el, w) => {
      if (!el || el.dataset.wfrozen !== '1') return
      el.style.width = ''
      delete el.dataset.wfrozen
      // Called from the end of a run, where every character has just locked
      // back to its real glyph - the one moment the natural width is known to
      // be the settled one. Reading it here is a synchronous layout flush of
      // a handful of spans, once per run.
      if (record) settledWidths.current[w] = el.getBoundingClientRect().width
    })
  }, [])

  const resolveAll = useCallback(() => {
    for (const el of charRefs.current) {
      if (!el) continue
      el.textContent = el.dataset.ch ?? el.textContent
      el.dataset.state = 'plain'
    }
    releaseWidths()
  }, [releaseWidths])

  /* Seed the settled-width cache before the first run can need it.
   *
   * At mount the words show their real glyphs, so they are measurable - but
   * only once the webfonts have actually loaded. Measured before that, every
   * width is the fallback font's and the first scramble would reserve the
   * wrong boxes, which is exactly the reflow this cache exists to stop. So it
   * waits on document.fonts.ready and takes the measurement then.
   */
  useEffect(() => {
    let cancelled = false
    const measure = () => {
      if (cancelled) return
      setFontsReady(true)
      wordRefs.current.forEach((el, w) => {
        // Never overwrite a width recorded at the end of a real run, and skip
        // wrappers the caller has already pinned.
        if (!el || el.style.width || settledWidths.current[w] != null) return
        settledWidths.current[w] = el.getBoundingClientRect().width
      })
    }
    const fonts = typeof document !== 'undefined' ? document.fonts : null
    if (fonts?.ready) fonts.ready.then(measure)
    else measure()
    return () => { cancelled = true }
  }, [text])

  const play = useCallback(() => {
    const rng = makeRng(seed + runRef.current * 7919)
    runRef.current += 1
    stop()

    const cells = charRefs.current.filter(Boolean)
    if (cells.length === 0) return

    lastStartRef.current = performance.now()
    playedRef.current = true
    onRunStartRef.current?.()

    // Restore the real glyphs BEFORE measuring. play() can be re-entered while
    // an earlier run is still on screen (stop() cancels the frame loop but
    // leaves the scrambled text in place), and measuring then froze each word
    // at its SCRAMBLED width - locking in the very reflow this prevents. The
    // symptom was words reading back wider than settled: 70.2 -> 71.5,
    // 82.1 -> 88.6, 162.8 -> 170.3.
    for (const el of cells) {
      el.textContent = el.dataset.ch ?? el.textContent
      el.dataset.state = 'plain'
    }
    releaseWidths()
    freezeWidths()

    const lockAt = new Float64Array(cells.length)
    const nextAt = new Float64Array(cells.length)
    const locked = new Uint8Array(cells.length)
    cells.forEach((el, idx) => {
      lockAt[idx] = startDelay + idx * stagger + (rng() * 2 - 1) * jitter
      nextAt[idx] = 0
      el.dataset.state = 'scramble'
      el.textContent = pool.charAt((rng() * pool.length) | 0)
    })

    let remaining = cells.length
    const t0 = performance.now()

    const frame = () => {
      const now = performance.now() - t0
      for (let idx = 0; idx < cells.length; idx += 1) {
        if (locked[idx]) continue
        const el = cells[idx]
        if (now >= lockAt[idx]) {
          el.textContent = el.dataset.ch ?? ''
          el.dataset.state = 'lock'
          locked[idx] = 1
          remaining -= 1
        } else if (now >= nextAt[idx]) {
          el.textContent = pool.charAt((rng() * pool.length) | 0)
          nextAt[idx] = now + speed + rng() * CYCLE_SPREAD
        }
      }
      if (remaining <= 0) {
        rafRef.current = null
        // Back to normal inline text between runs, so nothing about the
        // settled headline depends on this having run at all - and this is
        // where the settled widths are recorded for the next run.
        releaseWidths(true)
        onDecryptedRef.current?.()
        if (loop !== false && loop > 0) {
          timerRef.current = setTimeout(() => {
            timerRef.current = null
            play()
          }, loop)
        }
        return
      }
      rafRef.current = requestAnimationFrame(frame)
    }
    rafRef.current = requestAnimationFrame(frame)
  }, [freezeWidths, jitter, loop, pool, releaseWidths, seed, speed, stagger, startDelay, stop])

  useLayoutEffect(() => {
    if (reduce) { stop(); resolveAll(); return }
    if (!visible) { stop(); return }
    if (trigger === 'hover') {
      if (!playedRef.current) resolveAll()
      return
    }
    // Nothing runs until the webfonts have loaded. Two reasons: a scramble
    // drawn in the fallback face is not the effect anyone asked for, and the
    // settled-width cache is measured at that same moment - starting first
    // would reserve every word at the fallback's width and reflow the
    // headline for the whole run.
    if (!fontsReady) return
    if (!playedRef.current) { play(); return }
    // Returning on-screen after a finished run re-arms the loop rather than
    // restarting mid-flight.
    if (loop !== false && loop > 0 && rafRef.current == null && timerRef.current == null) {
      timerRef.current = setTimeout(() => { timerRef.current = null; play() }, Math.min(loop, 3000))
    }
  }, [fontsReady, loop, play, reduce, resolveAll, stop, trigger, visible])

  useEffect(() => stop, [stop])

  const onPointerEnter = useCallback(() => {
    if (reduce || !retriggerOnHover) return
    if (rafRef.current != null) return
    if (performance.now() - lastStartRef.current < HOVER_COOLDOWN) return
    play()
  }, [play, reduce, retriggerOnHover])

  const terminal = variant === 'terminal'
  const scrambleColor = terminal ? CHROME.accentText : CHROME.inkMuted
  const lockedColor = terminal ? CHROME.inkSecondary : CHROME.ink

  // A coloured word keeps its colour once locked; while scrambling every
  // character stays muted, so the colour "arrives" with the real letter.
  const colorForWord = (w) => (wordColors && wordColors[w]) || null
  const styleForWord = (w) => (wordStyles && wordStyles[w]) || null
  const wrapStyleForWord = (w) => (wordWrapStyles && wordWrapStyles[w]) || null

  const css = `
.${scope} [data-ch]{color:var(--dt-tint, ${lockedColor});}
.${scope} [data-ch][data-state="scramble"]{color:${scrambleColor};}
.${scope} [data-ch][data-state="lock"]{color:var(--dt-tint, ${lockedColor});animation:${scope}-flash ${FLASH_MS}ms cubic-bezier(.2,0,0,1);}
@keyframes ${scope}-flash{0%{color:${CHROME.accentText};text-shadow:0 0 24px ${CHROME.accent}80;}100%{text-shadow:0 0 0 transparent;}}
.${scope} [data-caret]{animation:${scope}-caret 1.1s steps(1) infinite;}
@keyframes ${scope}-caret{50%{opacity:0;}}
@media (prefers-reduced-motion: reduce){.${scope} [data-ch][data-state="lock"],.${scope} [data-caret]{animation:none;}}
`

  let cursor = -1
  const glyphLayer = (
    <span aria-hidden="true" className="select-none">
      {words.map((word, w) => (
        <span key={w}>
          <span
            className="inline-block whitespace-pre"
            style={wrapStyleForWord(w) || undefined}
            ref={(el) => { wordRefs.current[w] = el }}
          >
            {word.map((item) => {
              cursor += 1
              const at = cursor
              const tint = colorForWord(w)
              const extra = styleForWord(w)
              const charStyle = (tint || extra)
                // --dt-tint is a custom property so the scramble/lock rules in
                // the stylesheet can still override the colour mid-animation.
                ? { ...(tint ? { '--dt-tint': tint } : {}), ...(extra || {}) }
                : undefined
              return (
                <span
                  key={item.i}
                  data-ch={item.ch}
                  data-state="plain"
                  style={charStyle}
                  ref={(el) => { charRefs.current[at] = el }}
                >
                  {item.ch}
                </span>
              )
            })}
          </span>
          {w < words.length - 1 ? ' ' : null}
        </span>
      ))}
    </span>
  )

  return (
    <Tag
      ref={rootRef}
      data-motion={reduce ? 'static' : 'animated'}
      onPointerEnter={onPointerEnter}
      className={
        (terminal
          ? 'block font-mono text-[clamp(0.78rem,2.4vw,1rem)] leading-relaxed '
          : 'block text-balance leading-[1.12] tracking-tight ') + className
      }
      style={style}
      {...rest}
    >
      <style>{css}</style>
      {/* The real string, for assistive tech and for select/copy. */}
      <span className="sr-only">{text}</span>
      {terminal ? (
        <span
          className={`${scope} inline-flex max-w-full flex-wrap items-baseline gap-x-1 px-3 py-2 align-middle`}
          style={{ border: `1px solid ${CHROME.border}`, background: CHROME.surface }}
        >
          <span aria-hidden="true" style={{ color: CHROME.accentText }}>$</span>
          {glyphLayer}
          <span
            aria-hidden="true"
            data-caret=""
            className="inline-block h-[1.05em] w-[0.5em] align-text-bottom"
            style={{ background: CHROME.accentText }}
          />
        </span>
      ) : (
        <span className={`${scope} block`}>{glyphLayer}</span>
      )}
    </Tag>
  )
}
