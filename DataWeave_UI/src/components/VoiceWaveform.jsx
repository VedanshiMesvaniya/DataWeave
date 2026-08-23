import { useEffect, useId, useRef } from 'react'

// How many envelope points span the width — this is a scrolling history
// buffer of recent loudness, not a single instant reading, which is what
// gives the shape multiple lobes across the box instead of one static blob.
const HISTORY_LENGTH = 64
const VIEWBOX_WIDTH = 100
const VIEWBOX_HEIGHT = 40
const CENTER_Y = VIEWBOX_HEIGHT / 2
// Headroom so a loud peak doesn't touch the top/bottom edge of the box.
const MAX_AMPLITUDE = CENTER_Y - 3
// How quickly the envelope eases toward a new loudness reading (0..1) — low
// enough that the blob swells/settles smoothly instead of jittering frame
// to frame with raw mic noise.
const SMOOTHING = 0.35
// RMS values from natural speech land well under 1.0, so this boosts them
// into a visually lively range without needing the person to shout.
const LOUDNESS_GAIN = 3.2

// Turns a flat point list into a smooth closed SVG path using the
// "quadratic through midpoints" trick: each segment's control point is the
// raw data point and the curve passes through the midpoint between
// consecutive points, giving an organic, rounded outline cheaply enough to
// recompute every animation frame.
function smoothClosedPathD(points) {
  if (points.length === 0) return ''
  let d = `M ${points[0].x.toFixed(2)} ${points[0].y.toFixed(2)}`
  for (let i = 1; i < points.length; i++) {
    const prev = points[i - 1]
    const curr = points[i]
    const midX = (prev.x + curr.x) / 2
    const midY = (prev.y + curr.y) / 2
    d += ` Q ${prev.x.toFixed(2)} ${prev.y.toFixed(2)} ${midX.toFixed(2)} ${midY.toFixed(2)}`
  }
  const last = points[points.length - 1]
  d += ` L ${last.x.toFixed(2)} ${last.y.toFixed(2)} Z`
  return d
}

const FLAT_LINE_D = `M 0 ${CENTER_Y} L ${VIEWBOX_WIDTH} ${CENTER_Y} Z`

/**
 * Siri-style filled waveform blob shown in place of the text input while
 * recording. A thin gradient line sits flat at rest and swells into smooth,
 * rounded lobes wherever recent mic loudness (RMS of `getWaveform()`'s
 * samples) is higher — built from a small scrolling history buffer rather
 * than a single instant reading, so multiple lobes move across the box the
 * way the reference animation does.
 *
 * Colors are drawn entirely from the app's own theme variables
 * (`--blob-a`/`--blob-b`/`--blob-c`/`--accent`, the same palette used for
 * the ambient background blobs elsewhere), so this reads as on-brand and
 * switches automatically for light/dark — no colors are hardcoded here.
 *
 * The `d` attribute is written directly to two <path> elements (a crisp fill
 * and a blurred glow layer beneath it) inside a requestAnimationFrame loop,
 * not via React state, since this repaints 50-60x/sec.
 */
export default function VoiceWaveform({ getWaveform, active }) {
  const pathRef = useRef(null)
  const glowRef = useRef(null)
  const frameRef = useRef(null)
  const historyRef = useRef(null)
  // useId gives a stable, render-pure identifier (unlike Math.random(),
  // which the React Compiler flags as an impure render call); strip colons
  // since useId's default format (":r0:") isn't a valid raw fragment id in
  // every browser when interpolated into a url(#...) reference.
  const gradientId = `composer-waveform-gradient-${useId().replace(/:/g, '')}`

  useEffect(() => {
    if (!active) return undefined
    historyRef.current = new Array(HISTORY_LENGTH).fill(0)

    const animate = () => {
      const samples = getWaveform?.() || []
      let level = 0
      if (samples.length) {
        let sumSquares = 0
        for (let i = 0; i < samples.length; i++) sumSquares += samples[i] * samples[i]
        const rms = Math.sqrt(sumSquares / samples.length)
        level = Math.min(1, rms * LOUDNESS_GAIN)
      }

      const history = historyRef.current
      const prevLevel = history[history.length - 1]
      const eased = prevLevel + (level - prevLevel) * SMOOTHING
      history.push(eased)
      history.shift()

      if (pathRef.current) {
        const step = VIEWBOX_WIDTH / (HISTORY_LENGTH - 1)
        const top = history.map((v, i) => ({ x: i * step, y: CENTER_Y - v * MAX_AMPLITUDE }))
        const bottom = history
          .map((v, i) => ({ x: i * step, y: CENTER_Y + v * MAX_AMPLITUDE }))
          .reverse()
        const d = smoothClosedPathD([...top, ...bottom])
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

  return (
    <svg
      className="composer__waveform-full"
      viewBox={`0 0 ${VIEWBOX_WIDTH} ${VIEWBOX_HEIGHT}`}
      preserveAspectRatio="none"
      aria-hidden="true"
    >
      <defs>
        <linearGradient id={gradientId} x1="0" y1="0" x2="1" y2="0">
          <stop offset="0%" stopColor="var(--text-secondary)" stopOpacity="0.35" />
          <stop offset="20%" stopColor="var(--blob-a)" />
          <stop offset="40%" stopColor="var(--accent)" />
          <stop offset="60%" stopColor="var(--blob-b)" />
          <stop offset="80%" stopColor="var(--blob-c)" />
          <stop offset="100%" stopColor="var(--text-secondary)" stopOpacity="0.35" />
        </linearGradient>
      </defs>
      {/* Soft blurred layer beneath the crisp fill gives the glow seen in
          the reference — same gradient, lower opacity, blurred. */}
      <path ref={glowRef} className="composer__waveform-glow" d={FLAT_LINE_D} fill={`url(#${gradientId})`} />
      <path ref={pathRef} className="composer__waveform-blob" d={FLAT_LINE_D} fill={`url(#${gradientId})`} />
    </svg>
  )
}
