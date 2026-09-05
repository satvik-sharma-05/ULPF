import { useEffect, useRef, useState } from 'react'

/* Small animation primitives, hand-rolled rather than pulling in
 * framer-motion: this app ships as a Docker image to an airgapped VM, and
 * these three behaviours are a few lines each.
 *
 * Every one of them honours prefers-reduced-motion by jumping straight to the
 * final state - motion here is decoration, and a user who has asked for less
 * of it should still get the numbers immediately.
 */

export function usePrefersReducedMotion() {
  const [reduced, setReduced] = useState(
    () => typeof window !== 'undefined'
      && window.matchMedia('(prefers-reduced-motion: reduce)').matches,
  )
  useEffect(() => {
    const mq = window.matchMedia('(prefers-reduced-motion: reduce)')
    const onChange = (e) => setReduced(e.matches)
    mq.addEventListener('change', onChange)
    return () => mq.removeEventListener('change', onChange)
  }, [])
  return reduced
}

/** True once the element has been scrolled into view (latches - it does not
 *  flip back when scrolled away, so a panel never re-animates on every pass). */
export function useInView(options = {}) {
  const ref = useRef(null)
  const [inView, setInView] = useState(false)

  useEffect(() => {
    const el = ref.current
    if (!el || typeof IntersectionObserver === 'undefined') { setInView(true); return undefined }
    const io = new IntersectionObserver(([entry]) => {
      if (entry.isIntersecting) { setInView(true); io.disconnect() }
    }, { threshold: 0.15, ...options })
    io.observe(el)
    return () => io.disconnect()
  }, [])

  return [ref, inView]
}

/** Eases a number up to `value`. Returns the current value to render.
 *
 * Kept short (550ms) on purpose: the dashboard's aggregate call can take a
 * couple of seconds, so the count-up starts whenever the data lands. A long
 * ramp means there is a visible window where a headline figure reads far too
 * low - which, on a screen someone is being shown, is worse than having no
 * animation at all. */
export function useCountUp(value, duration = 550) {
  const reduced = usePrefersReducedMotion()
  const [display, setDisplay] = useState(reduced ? value : 0)
  const fromRef = useRef(0)

  useEffect(() => {
    const target = Number(value) || 0
    if (reduced) { setDisplay(target); return undefined }
    const from = fromRef.current
    const start = performance.now()
    let raf
    const tick = (now) => {
      const t = Math.min((now - start) / duration, 1)
      // easeOutCubic: fast then settling, which reads as "counted up" rather
      // than "scrolled past".
      const eased = 1 - Math.pow(1 - t, 3)
      setDisplay(from + (target - from) * eased)
      if (t < 1) raf = requestAnimationFrame(tick)
      else fromRef.current = target
    }
    raf = requestAnimationFrame(tick)
    return () => cancelAnimationFrame(raf)
  }, [value, duration, reduced])

  return display
}

/** Fades + lifts its children in once scrolled into view, with an optional
 *  stagger index so a grid of cards arrives in sequence. */
export function Reveal({ children, delay = 0, className = '', style }) {
  const reduced = usePrefersReducedMotion()
  const [ref, inView] = useInView()
  const on = reduced || inView

  return (
    <div
      ref={ref}
      className={className}
      style={{
        ...style,
        opacity: on ? 1 : 0,
        transform: on ? 'none' : 'translateY(10px)',
        transition: reduced ? 'none' : `opacity 460ms ease ${delay}ms, transform 460ms cubic-bezier(.2,.7,.3,1) ${delay}ms`,
      }}
    >
      {children}
    </div>
  )
}
