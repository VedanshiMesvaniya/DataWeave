import { useEffect, useId, useRef } from 'react'

// How many points make up the line — enough to look smooth and continuous
// across a typical composer width, without being expensive to rebuild
// every animation frame.
const POINTS = 80
const VIEWBOX_WIDTH = 100
const VIEWBOX_HEIGHT = 32
const CENTER_Y = VIEWBOX_HEIGHT / 2
const MAX_AMPLITUDE = CENTER_Y - 3

/**
 * Full-width reactive line shown in place of the text input while
 * recording — a single continuous, glowing, gradient-colored stroke that
 * traces the mic's actual waveform shape (an oscilloscope-style readout via
 * a Web Audio `AnalyserNode`), so it genuinely moves with voice pitch and
 * loudness rather than looping a canned animation. Sits flat near the
 * composer's baseline at rest and swells where the signal is loud, spanning
 * the same full width as the input it replaces (roughly the same span the
 * left-side model selector and right-side mic button bookend).
 *
 * Colors are drawn entirely from the app's own theme variables
 * (`--blob-a`/`--blob-b`/`--blob-c`/`--accent`, the same palette used for
 * the ambient background blobs elsewhere), so this reads as on-brand and
 * switches automatically for light/dark — no colors are hardcoded here.
 *
 * The `d` attribute is written directly to the path (a crisp stroke plus a
 * blurred glow copy beneath it) inside a requestAnimationFrame loop, not
 * via React state, since this repaints 50-60x/sec.
 */
export default function VoiceWaveform({ getWaveform, active }) {
  const pathRef = useRef(null)
  const glowRef = useRef(null)
  const frameRef = useRef(null)
  // useId gives a stable, render-pure identifier (Math.random() during
  // render is flagged as impure by this repo's stricter hook-purity lint).
  const gradientId = `composer-waveform-gradient-${useId().replace(/:/g, '')}`

  useEffect(() => {
    if (!active) return undefined

    const animate = () => {
      const samples = getWaveform?.() || []
      if (samples.length && pathRef.current) {
        const step = VIEWBOX_WIDTH / (POINTS - 1)
        let d = ''
        for (let i = 0; i < POINTS; i++) {
          const sampleIndex = Math.floor((i / (POINTS - 1)) * (samples.length - 1))
          const value = samples[sampleIndex] ?? 0 // -1..1
          const x = i * step
          const y = CENTER_Y - value * MAX_AMPLITUDE
          d += i === 0 ? `M ${x.toFixed(2)} ${y.toFixed(2)}` : ` L ${x.toFixed(2)} ${y.toFixed(2)}`
        }
        pathRef.current.setAttribute('d', d)
        glowRef.current?.setAttribute('d', d)
      }
      frameRef.current = requestAnimationFrame(animate)
    }

    frameRef.current = requestAnimationFrame(animate)
    return () => {
      if (frameRef.current) cancelAnimationFrame(frameRef.current)
    }
  }, [active, getWaveform])

  if (!active) return null

  const flatD = `M 0 ${CENTER_Y} L ${VIEWBOX_WIDTH} ${CENTER_Y}`

  return (
    <svg
      className="composer__waveform-full"
      viewBox={`0 0 ${VIEWBOX_WIDTH} ${VIEWBOX_HEIGHT}`}
      preserveAspectRatio="none"
      aria-hidden="true"
    >
      <defs>
        <linearGradient id={gradientId} x1="0" y1="0" x2="1" y2="0">
          <stop offset="0%" stopColor="var(--text-muted)" />
          <stop offset="22%" stopColor="var(--blob-a)" />
          <stop offset="48%" stopColor="var(--accent)" />
          <stop offset="72%" stopColor="var(--blob-b)" />
          <stop offset="100%" stopColor="var(--blob-c)" />
        </linearGradient>
      </defs>
      {/* Soft blurred copy beneath the crisp stroke for the glow — same
          gradient/path, lower opacity, blurred. */}
      <path
        ref={glowRef}
        className="composer__waveform-glow"
        d={flatD}
        fill="none"
        stroke={`url(#${gradientId})`}
      />
      <path
        ref={pathRef}
        className="composer__waveform-line"
        d={flatD}
        fill="none"
        stroke={`url(#${gradientId})`}
      />
    </svg>
  )
}
