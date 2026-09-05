import { useEffect, useState } from 'react'
import DotGridBackground from '../components/ui/DotGridBackground.jsx'
import DecryptText from '../components/ui/DecryptText.jsx'
import { Reveal } from '../components/ui/motion.jsx'
import { CHROME, SERIES, severityMeta } from '../components/analytics/theme.js'

/* The marketing surface: what this platform is and what it does.
 *
 * Deliberately carries no live data and no chat box - product surfaces belong
 * on the pages built for them (Analytics, Alerts, Live logs, Chat). A landing
 * page that also queries the database is two things done adequately instead of
 * one done well.
 */

const CAPABILITIES = [
  {
    n: '30+',
    title: 'Parse anything',
    body: 'Format detectors across VMware SDDC, Kubernetes, Linux and Windows — plus Syslog, JSON, XML, CSV, CEF and LEEF on upload. Nothing is dropped: unknown shapes fall through to a generic extractor.',
  },
  {
    n: '34',
    title: 'Typed entities',
    body: 'Hosts, users, devices, VMs, pods, traces and operations pulled out of free text — with 25+ derived relationships, so a path between two facts never has to detour through a log line.',
  },
  {
    n: '1024',
    title: 'Dimensions of meaning',
    body: 'BAAI/bge-m3 embeddings on every log, in a native Neo4j vector index. “Why did storage fail” finds the right lines even when they share none of your words.',
  },
  {
    n: '0',
    title: 'Outbound calls',
    body: 'Models baked into the images, a local LLM on the box. The same build runs online with a cloud model when you have the network, and fully airgapped when you don’t.',
  },
]

const PIPELINE = [
  { step: '01', name: 'Ingest', detail: 'Batch files, a Kafka stream, or a direct upload' },
  { step: '02', name: 'Parse', detail: 'Detector chain → normalized record + typed entities' },
  { step: '03', name: 'Embed', detail: 'BAAI/bge-m3, 1024-dim, into a native vector index' },
  { step: '04', name: 'Answer', detail: 'Graph traversal + semantic retrieval + synthesis' },
]

// One darkened palette step per headline word, in reading order:
// Universal / Log / Pre-processing / Framework. The vivid series hues are
// fills, not text - every value here is its darkened twin, measured on the
// white hero surface: violet 7.12:1, orange 5.18:1, green 5.33:1, blue
// 6.09:1. All four clear 4.5:1, so the headline is colourful without any word
// being decoration you have to squint at. "Log" takes the orange because it
// is the word that flickers, and the brand colour is the one to land it on.
const HEADLINE_WORDS = ['#5F3DC4', '#C2410C', '#2B7A38', '#1864AB']

// The word "Log" flickers between typefaces. Only faces the app actually
// bundles are used - naming a font that isn't loaded would silently fall back
// and look like nothing happened. Fraunces (--font-fun) is the wonky serif
// that makes the cycle read as playful rather than as a rendering glitch.
const LOGS_FACES = [
  { fontFamily: 'var(--font-display)' },
  { fontFamily: 'var(--font-fun)', fontWeight: 900 },
  { fontFamily: 'var(--font-mono)', letterSpacing: '-0.05em' },
  { fontFamily: 'var(--font-alt)', fontStyle: 'italic' },
  { fontFamily: 'var(--font-fun)', fontStyle: 'italic', fontWeight: 700 },
  { fontFamily: 'var(--font-display)', fontStyle: 'italic', fontWeight: 900 },
  { fontFamily: 'var(--font-mono)', fontStyle: 'italic', letterSpacing: '-0.05em' },
  { fontFamily: 'var(--font-sans)' },
]
const FACE_MS = 250   // 4 faces a second

// The faces differ in width - mono is far wider than the display face - so the
// wrapper is pinned to a width that fits the widest of them and the glyphs are
// centred inside it. Without this the two words after "logs" would jitter
// left and right four times a second, which reads as a broken layout rather
// than an effect. `em` so it tracks the responsive headline size.
// The pinned width is the MEASURED width of "Log" in the widest face of the
// cycle, at the live headline size. Pinning to anything larger just opens
// dead space either side of the word, which is what a guessed value did
// before this was measured - see LOGS_WIDTH_EM.
/* The box "Log" lives in is FIXED in both axes, and that is what keeps the
 * page still.
 *
 * Width (2.24em) is the measured width of the widest face in the cycle, so
 * the words after it never shift sideways. Height plus `overflow: hidden` is
 * the less obvious half: for an inline-block with hidden overflow the CSS
 * baseline becomes the bottom margin edge rather than the baseline of the
 * text inside it, which makes the box's contribution to the line completely
 * independent of the swapped font's ascent and descent. Without it the h1
 * still breathed by ~7px per face change even with the line-height pinned.
 *
 * The height is generous enough that the descender on "g" sits inside the box
 * rather than being clipped by the same `overflow: hidden`.
 */
// Measured width of the flickering word in the WIDEST face of the cycle,
// at the live headline size: Fraunces 900 measures 1.827em, JetBrains Mono
// the narrowest at 1.65em. 1.86em clears the widest with a hair of slack.
const LOGS_WIDTH_EM = 1.86
const LOGS_WRAP = {
  display: 'inline-block',
  // A hard width, not min-width: min-width still lets the box GROW to fit a
  // wider face, and a box that grows changes where the line breaks. At 390px
  // a growing box moved the following word onto a new line and back, four
  // times a second. The value is MEASURED - see LOGS_WIDTH_EM below.
  width: `${LOGS_WIDTH_EM}em`,
  height: '1.05em',
  overflow: 'hidden',
  lineHeight: '1.05em',
  textAlign: 'center',
  verticalAlign: 'baseline',
}

const FORMATS = ['Syslog', 'RFC 3164', 'RFC 5424', 'JSON', 'JSONL', 'XML', 'CSV', 'CEF', 'LEEF',
                 'ESXi', 'NSX', 'vCenter', 'vROps', 'Horizon', 'Kubernetes', 'CoreDNS',
                 'PostgreSQL', 'MinIO', 'Squid', 'JVM GC', 'auditd', 'Windows Security']

/* A miniature of the real thing - same severity colours, glyphs and mono face
 * the product uses, so the preview is honest about what you actually get.
 *
 * The hostnames are generic ON PURPOSE. This previously carried four real log
 * lines captured from a live estate, which shipped inside the handover package
 * and, worse, rendered on a freshly deployed instance that had ingested
 * nothing - four convincing rows implying data that was not there. The label
 * above it says "example" for the same reason.
 */
const PREVIEW = [
  { sev: 'ERROR', host: 'esxi-host-01', msg: "Unknown feature 'VMODL_LIFECYCLE'. ASSUMING DISABLED" },
  { sev: 'WARNING', host: 'nsx-manager-01', msg: 'Not a leader, ignoring scheduled task' },
  { sev: 'ERROR', host: 'unknown-host', msg: '"ReopenContainerLog from runtime service failed"' },
  { sev: 'INFO', host: 'app-node-07', msg: 'Purging log events older than 30 days' },
]

export default function HomePage({ onNavigate }) {
  const [face, setFace] = useState(0)

  // Its own fast timer, independent of the scramble loop. Paused when the tab
  // is hidden - a 4Hz re-render in a background tab is pure battery drain -
  // and skipped entirely under prefers-reduced-motion, where a strobing
  // headline is exactly the kind of thing that setting exists to stop.
  useEffect(() => {
    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return undefined
    let id = null
    const start = () => { id = id ?? setInterval(() => setFace((f) => (f + 1) % LOGS_FACES.length), FACE_MS) }
    const stop = () => { if (id) { clearInterval(id); id = null } }
    const onVis = () => (document.visibilityState === 'hidden' ? stop() : start())
    start()
    document.addEventListener('visibilitychange', onVis)
    return () => { stop(); document.removeEventListener('visibilitychange', onVis) }
  }, [])

  return (
    <div className="h-full overflow-y-auto relative" style={{ background: CHROME.pagePlane }}>

      {/* ---------- Hero ---------- */}
      <section className="relative" style={{ background: CHROME.surface }}>
        <DotGridBackground />
        <div className="relative z-10 px-6 pt-20 pb-16 max-w-6xl mx-auto">
          <div className="flex flex-col items-center text-center">
            {/* Every word gets its own palette hue rather than a flat black
                line. All four are darkened variants of the series steps, so
                they clear 4.5:1 as headline text - the vivid steps do not.

                "Log" flickers between typefaces four times a second. Its
                wrapper is width-pinned (LOGS_WRAP) so only the letterforms
                change - otherwise the words after it would jitter. */}
            <DecryptText
              as="h1"
              text="Universal Log Pre-processing Framework"
              trigger="mount"
              stagger={34}
              startDelay={180}
              loop={10000}
              wordColors={HEADLINE_WORDS}
              wordStyles={[null, LOGS_FACES[face], null, null]}
              wordWrapStyles={[null, LOGS_WRAP, null, null]}
              // leading-[1.05] is load-bearing, not styling. With the default
              // `line-height: normal` the line box is sized from the CURRENT
              // font's ascent/descent, and the face under "logs" changes four
              // times a second - so the h1 was measured swinging between
              // 112.2px and 119.2px and shoving the whole page up and down at
              // 4Hz. A numeric line-height derives the strut from font-size
              // alone, which does not change.
              className="text-[clamp(2.4rem,7.5vw,5rem)] leading-[1.05] font-extrabold pt-4"
              // The headline's own height is RESERVED, and this is the fix for
              // the page bouncing at 4Hz.
              //
              // `line-height: normal` sized the line box from each font's
              // ascent/descent, so the h1 measured 112.2 / 115.2 / 119.2px as
              // the face under "Log" cycled, shoving everything below it up
              // and down. leading-[1.05] pins the strut but NOT the baseline
              // offset each face wants, which still left a 7px swing - so the
              // box is reserved outright at the tallest face's height
              // (measured: 1.333em including the pt-4, hence 1.34em).
              //
              // min-height, not height: if the headline ever wraps to two
              // lines on a narrow viewport this stops binding and the element
              // grows normally instead of overflowing.
              // textWrap 'wrap' overrides DecryptText's `text-balance`. The
              // balancer re-solves line breaks from the CURRENT text metrics,
              // so at narrow widths a wider face pushed "logs" onto a third
              // line and the headline grew by a whole line, four times a
              // second. Greedy wrapping plus the fixed-size word box below
              // makes the break points deterministic.
              // overflowWrap: a viewport narrow enough that
              // "Pre-processing" does not fit on a line should break the
              // word, not push the whole page sideways into a horizontal
              // scrollbar.
              style={{ minHeight: '1.34em', textWrap: 'wrap', overflowWrap: 'break-word' }}
            />

            <p className="mt-6 max-w-2xl text-base leading-relaxed" style={{ color: CHROME.inkSecondary }}>
              A log analytics platform that turns raw infrastructure logs into a Neo4j knowledge
              graph — typed entities, real relationships and semantic embeddings — so root-cause
              questions have somewhere to land.
            </p>

            <div className="flex flex-wrap items-center justify-center gap-2.5 mt-9">
              <button
                onClick={() => onNavigate?.('chat')}
                className="px-6 py-3 text-sm font-semibold transition-transform hover:-translate-y-0.5"
                style={{ background: CHROME.primary, color: CHROME.primaryInk,
                         boxShadow: '0 2px 8px rgba(10,10,10,0.18)' }}
              >
                Start asking →
              </button>
              <button
                onClick={() => onNavigate?.('analytics')}
                className="px-6 py-3 text-sm font-semibold transition-colors"
                style={{ background: CHROME.surface, border: `1px solid ${CHROME.borderStrong}`, color: CHROME.ink }}
              >
                View analytics
              </button>
              <button
                onClick={() => onNavigate?.('modes')}
                className="px-6 py-3 text-sm font-semibold transition-colors"
                style={{ background: CHROME.surface, border: `1px solid ${CHROME.border}`, color: CHROME.inkSecondary }}
              >
                Ingest your logs
              </button>
            </div>
          </div>

          {/* Product preview - a real-looking slice of the log table */}
          <Reveal delay={120}>
            <div
              className="mt-14 overflow-hidden mx-auto max-w-3xl"
              style={{ border: `1px solid ${CHROME.border}`, background: CHROME.surface,
                       boxShadow: '0 12px 32px -12px rgba(10,10,10,0.18)' }}
            >
              <div className="flex items-center gap-2 px-4 py-2.5"
                   style={{ background: CHROME.surfaceRaised, borderBottom: `1px solid ${CHROME.border}` }}>
                <span className="flex gap-1.5">
                  {[SERIES[0], SERIES[2], SERIES[3]].map((c) => (
                    <span key={c} className="w-2.5 h-2.5" style={{ background: c }} />
                  ))}
                </span>
                <span className="text-[11px] font-mono ml-2" style={{ color: CHROME.inkMuted }}>
                  example logs
                </span>
              </div>
              {PREVIEW.map((r, i) => {
                const meta = severityMeta(r.sev)
                return (
                  <div
                    key={i}
                    className="flex items-center gap-3 px-4 py-2 text-[12px]"
                    style={{ borderTop: i ? `1px solid ${CHROME.gridline}` : 'none' }}
                  >
                    <span className="font-mono shrink-0" style={{ color: CHROME.inkDim }}>
                      05:29:5{i}
                    </span>
                    <span className="inline-flex items-center gap-1 font-semibold shrink-0 w-[76px]"
                          style={{ color: meta.textColor, fontSize: 11 }}>
                      <span style={{ color: meta.color }}>{meta.glyph}</span>{r.sev}
                    </span>
                    <span className="font-mono shrink-0 hidden sm:inline" style={{ color: CHROME.inkSecondary }}>
                      {r.host}
                    </span>
                    <span className="truncate" style={{ color: CHROME.ink }}>{r.msg}</span>
                  </div>
                )
              })}
            </div>
          </Reveal>
        </div>
      </section>

      {/* ---------- Supported formats ---------- */}
      <section className="px-6 py-10" style={{ borderTop: `1px solid ${CHROME.border}` }}>
        <div className="max-w-6xl mx-auto">
          <p className="text-[11px] uppercase tracking-widest mb-4 text-center" style={{ color: CHROME.inkMuted }}>
            Parses out of the box
          </p>
          <div className="flex flex-wrap justify-center gap-2">
            {FORMATS.map((f, i) => (
              <span
                key={f}
                className="px-3 py-1.5 text-xs font-medium"
                style={{
                  background: CHROME.surface,
                  border: `1px solid ${CHROME.border}`,
                  color: CHROME.inkSecondary,
                  animation: `fm-in 380ms ease both ${i * 28}ms`,
                }}
              >
                {f}
              </span>
            ))}
          </div>
          <style>{`@keyframes fm-in{from{opacity:0;transform:translateY(5px)}to{opacity:1;transform:none}}`}</style>
        </div>
      </section>

      {/* ---------- Capabilities ---------- */}
      <section className="px-6 py-16" style={{ borderTop: `1px solid ${CHROME.border}` }}>
        <div className="max-w-6xl mx-auto">
          <h2 className="text-2xl font-bold mb-1" style={{ color: CHROME.ink }}>What it does</h2>
          <p className="text-sm mb-9" style={{ color: CHROME.inkMuted }}>
            Four things, each of which the graph is what makes possible.
          </p>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            {CAPABILITIES.map((c, i) => (
              <Reveal key={c.title} delay={i * 70}>
                <div
                  className="p-6 h-full transition-transform hover:-translate-y-0.5"
                  style={{ background: CHROME.surface, border: `1px solid ${CHROME.border}` }}
                >
                  <div className="text-3xl font-extrabold mb-3 tabular"
                       style={{ color: SERIES[i % SERIES.length], fontFamily: 'var(--font-display)' }}>
                    {c.n}
                  </div>
                  <h3 className="text-base font-bold mb-2" style={{ color: CHROME.ink }}>{c.title}</h3>
                  <p className="text-sm leading-relaxed" style={{ color: CHROME.inkSecondary }}>{c.body}</p>
                </div>
              </Reveal>
            ))}
          </div>
        </div>
      </section>

      {/* ---------- Pipeline ---------- */}
      <section className="px-6 py-16" style={{ borderTop: `1px solid ${CHROME.border}`, background: CHROME.surface }}>
        <div className="max-w-6xl mx-auto">
          <h2 className="text-2xl font-bold mb-1" style={{ color: CHROME.ink }}>How it works</h2>
          <p className="text-sm mb-9" style={{ color: CHROME.inkMuted }}>
            One path, whichever mode the logs arrive through.
          </p>
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
            {PIPELINE.map((p, i) => (
              <Reveal key={p.step} delay={i * 70}>
                <div className="relative h-full">
                  <div
                    className="p-5 h-full"
                    style={{ background: CHROME.pagePlane, border: `1px solid ${CHROME.border}` }}
                  >
                    <div className="flex items-center gap-2 mb-3">
                      <span
                        className="w-7 h-7 flex items-center justify-center text-[11px] font-bold"
                        style={{ background: CHROME.primary, color: CHROME.primaryInk }}
                      >
                        {p.step}
                      </span>
                      <span className="text-base font-bold" style={{ color: CHROME.ink }}>{p.name}</span>
                    </div>
                    <div className="text-sm leading-relaxed" style={{ color: CHROME.inkMuted }}>{p.detail}</div>
                  </div>
                  {/* connector, on wide layouts only */}
                  {i < PIPELINE.length - 1 && (
                    <span className="hidden lg:block absolute top-1/2 -right-2 text-lg"
                          style={{ color: CHROME.borderStrong }} aria-hidden="true">›</span>
                  )}
                </div>
              </Reveal>
            ))}
          </div>
        </div>
      </section>

      {/* ---------- Closing CTA ---------- */}
      <section className="px-6 py-16" style={{ borderTop: `1px solid ${CHROME.border}` }}>
        <Reveal>
          <div
            className="max-w-3xl mx-auto text-center p-10"
            style={{ background: CHROME.surface, border: `1px solid ${CHROME.border}` }}
          >
            <h2 className="text-2xl font-bold mb-2" style={{ color: CHROME.ink }}>
              Point it at your logs
            </h2>
            <p className="text-sm mb-7 max-w-lg mx-auto leading-relaxed" style={{ color: CHROME.inkSecondary }}>
              Upload a file, connect a Kafka stream, or explore the bundled corpus.
              The parser, the entity extraction and the embeddings are identical either way.
            </p>
            <div className="flex flex-wrap items-center justify-center gap-2.5">
              <button
                onClick={() => onNavigate?.('modes')}
                className="px-6 py-3 text-sm font-semibold transition-transform hover:-translate-y-0.5"
                style={{ background: CHROME.primary, color: CHROME.primaryInk }}
              >
                Choose a mode
              </button>
              <button
                onClick={() => onNavigate?.('alerts')}
                className="px-6 py-3 text-sm font-semibold"
                style={{ background: CHROME.surface, border: `1px solid ${CHROME.borderStrong}`, color: CHROME.ink }}
              >
                See alerts
              </button>
            </div>
          </div>
        </Reveal>
      </section>

      <footer className="px-6 py-8 text-center text-xs"
              style={{ borderTop: `1px solid ${CHROME.border}`, color: CHROME.inkMuted }}>
        Neo4j knowledge graph · BAAI/bge-m3 embeddings · runs online or fully airgapped
      </footer>
    </div>
  )
}
