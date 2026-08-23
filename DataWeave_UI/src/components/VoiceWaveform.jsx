import { useEffect, useRef } from 'react'

// How many points make up the line — enough to look smooth and continuous
// (not jagged/dotted) across a typical composer width, without being
// expensive to rebuild every animation frame.
const POINTS = 80
const VIEWBOX_WIDTH = 100
const VIEWBOX_HEIGHT = 32
const CENTER_Y = VIEWBOX_HEIGHT / 2

/**
 * Full-width animated line shown in place of the text input while
 * recording — a single continuous stroke across the whole box that traces
 * the mic's actual waveform shape (an oscilloscope-style readout), so it
 * genuinely moves up and down with voice pitch/loudness rather than looping
 * a canned animation. Colors come entirely from CSS variables
 * (`--text-secondary`) already used elsewhere in the composer, so it themes
 * for light/dark automatically with no extra work here.
 *
 * The SVG uses a fixed viewBox with `preserveAspectRatio="none"` so it
 * stretches to fill the container at any width without needing a
 * ResizeObserver; `vector-effect="non-scaling-stroke"` keeps the line
 * thickness constant despite the non-uniform scale that implies.
 *
 * Animation runs via a direct `d` attribute write inside a
 * requestAnimationFrame loop, not React state — this repaints 50-60x/sec
 * and doing that through setState would re-render the whole composer every
 * frame.
 */
export default function VoiceWaveform({ getWaveform, active }) {
  const pathRef = useRef(null)
  const frameRef = useRef(null)

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
          const y = CENTER_Y - value * (CENTER_Y - 2)
          d += i === 0 ? `M ${x.toFixed(2)} ${y.toFixed(2)}` : ` L ${x.toFixed(2)} ${y.toFixed(2)}`
        }
        pathRef.current.setAttribute('d', d)
      }
      frameRef.current = requestAnimationFrame(animate)
    }

    frameRef.current = requestAnimationFrame(animate)
    return () => {
      if (frameRef.current) cancelAnimationFrame(frameRef.current)
    }
  }, [active, getWaveform])

  if (!active) return null

  return (
    <svg
      className="composer__waveform-full"
      viewBox={`0 0 ${VIEWBOX_WIDTH} ${VIEWBOX_HEIGHT}`}
      preserveAspectRatio="none"
      aria-hidden="true"
    >
      <path
        ref={pathRef}
        className="composer__waveform-line"
        d={`M 0 ${CENTER_Y} L ${VIEWBOX_WIDTH} ${CENTER_Y}`}
        fill="none"
      />
    </svg>
  )
}
