import { useEffect, useRef } from 'react'
import { SERIES } from '../analytics/theme.js'

/* Interactive dot-grid canvas, adapted from the Quordix hero.
 *
 * Changed from the original in three ways that matter here:
 *  - The app's own palette instead of the hardcoded #F8FAFC, so it belongs
 *    to this console rather than fighting it. The drifting particles and the
 *    hover halo cycle through all six series hues, which is what turns a flat
 *    grid of grey dots into something that feels alive under the cursor.
 *  - No Google Fonts import. The original pulls Space Grotesk over the
 *    network, which is a silent failure on the airgapped VM this ships to -
 *    the system sans stack is used instead.
 *  - The rAF loop stops when the tab is hidden. A full-viewport per-frame
 *    canvas that keeps running in a background tab is a laptop-battery bug.
 */
export default function DotGridBackground({ className = '', style }) {
  const canvasRef = useRef(null)

  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas) return undefined
    const ctx = canvas.getContext('2d')
    if (!ctx) return undefined

    const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches

    let raf = null
    let mouseX = -1000
    let mouseY = -1000
    let isMobile = false

    const SPACING = 30
    const BASE_R = 1.2
    const HOVER_R = 110
    const DEFAULT_COLOR = 'rgba(43,33,24,0.10)'   // warm, matches the cream plane

    // Hex -> rgba, so a palette step can be drawn at partial alpha without
    // keeping a second hand-written list of colours in sync with theme.js.
    const tint = (hex, a) => {
      const n = parseInt(hex.slice(1), 16)
      return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`
    }
    const PARTICLE_COLORS = SERIES.map((c) => tint(c, 0.22))

    class Particle {
      constructor(w, h) { this.reset(w, h) }
      reset(w, h) {
        this.x = Math.random() * w
        this.y = Math.random() * h
        this.vx = (Math.random() - 0.5) * 0.4
        this.vy = (Math.random() - 0.5) * 0.4
        this.size = Math.random() * 1.6
        this.color = PARTICLE_COLORS[Math.floor(Math.random() * PARTICLE_COLORS.length)]
      }
      update(w, h) {
        this.x += this.vx; this.y += this.vy
        if (this.x < 0 || this.x > w) this.vx *= -1
        if (this.y < 0 || this.y > h) this.vy *= -1
      }
      draw(c) {
        c.beginPath()
        c.arc(this.x, this.y, this.size, 0, Math.PI * 2)
        c.fillStyle = this.color
        c.fill()
      }
    }

    const particles = []
    const resize = () => {
      // Size in CSS pixels but back with devicePixelRatio, or the dots are
      // visibly soft on any retina/scaled display.
      const dpr = Math.min(window.devicePixelRatio || 1, 2)
      const w = canvas.clientWidth
      const h = canvas.clientHeight
      canvas.width = Math.floor(w * dpr)
      canvas.height = Math.floor(h * dpr)
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
      isMobile = window.innerWidth < 768
      particles.length = 0
      const n = isMobile ? 10 : 34
      for (let i = 0; i < n; i += 1) particles.push(new Particle(w, h))
    }

    const onMouseMove = (e) => {
      if (isMobile) return
      const r = canvas.getBoundingClientRect()
      mouseX = e.clientX - r.left
      mouseY = e.clientY - r.top
    }
    const onMouseLeave = () => { mouseX = -1000; mouseY = -1000 }

    const draw = () => {
      const w = canvas.clientWidth
      const h = canvas.clientHeight
      ctx.clearRect(0, 0, w, h)

      if (!reduced) {
        particles.forEach((p) => { p.update(w, h); p.draw(ctx) })
      }

      let cell = 0
      for (let x = 0; x < w; x += SPACING) {
        for (let y = 0; y < h; y += SPACING) {
          cell += 1
          const dx = x - mouseX
          const dy = y - mouseY
          const dist = Math.sqrt(dx * dx + dy * dy)
          if (dist < HOVER_R) {
            const scale = 1 - dist / HOVER_R
            ctx.fillStyle = SERIES[cell % SERIES.length]
            ctx.globalAlpha = 0.25 + scale * 0.75
            ctx.beginPath()
            ctx.arc(x, y, BASE_R + scale * 2.4, 0, Math.PI * 2)
            ctx.fill()
            ctx.globalAlpha = 1
          } else {
            ctx.fillStyle = DEFAULT_COLOR
            ctx.beginPath()
            ctx.arc(x, y, BASE_R, 0, Math.PI * 2)
            ctx.fill()
          }
        }
      }
      raf = requestAnimationFrame(draw)
    }

    const start = () => { if (raf == null) draw() }
    const pause = () => { if (raf != null) { cancelAnimationFrame(raf); raf = null } }
    const onVis = () => (document.visibilityState === 'hidden' ? pause() : start())

    window.addEventListener('resize', resize)
    window.addEventListener('mousemove', onMouseMove)
    window.addEventListener('mouseleave', onMouseLeave)
    document.addEventListener('visibilitychange', onVis)
    resize()
    start()

    return () => {
      window.removeEventListener('resize', resize)
      window.removeEventListener('mousemove', onMouseMove)
      window.removeEventListener('mouseleave', onMouseLeave)
      document.removeEventListener('visibilitychange', onVis)
      pause()
    }
  }, [])

  return (
    <canvas
      ref={canvasRef}
      aria-hidden="true"
      className={`absolute inset-0 w-full h-full block ${className}`}
      style={style}
    />
  )
}
