import { useEffect, useRef } from 'react'

// Enough bars to read as a continuous equalizer across a typical composer
// width without being expensive to update every animation frame.
const BAR_COUNT = 56
// How quickly each bar eases toward a new loudness reading (0..1) — low
// enough that bars rise/settle smoothly instead of jittering with raw mic
// noise, matching the reference's calmer, rounded motion.
const SMOOTHING = 0.35
// RMS values from natural speech land well under 1.0, so this boosts them
// into a visually lively range without needing the person to shout.
const LOUDNESS_GAIN = 3.4

/**
 * Bar-style live equalizer shown next to the "Listening..." label while
 * recording — a small scrolling history of mic loudness (RMS of
 * `getWaveform()`'s samples), rendered as individual bars, matching a
 * classic voice-recorder waveform look rather than a single smooth line.
 *
 * Bar heights/opacity are written directly to the DOM inside a
 * requestAnimationFrame loop, not via React state, since this repaints
 * 50-60x/sec and doing that through setState would re-render the whole
 * composer every frame.
 *
 * Color is a single theme accent (`var(--accent)`), varying only in
 * opacity/height with loudness — matching the reference's uniformly blue
 * bars rather than a multi-hue gradient.
 */
export default function VoiceWaveform({ getWaveform, active }) {
  const barRefs = useRef([])
  const frameRef = useRef(null)
  const historyRef = useRef(null)

  useEffect(() => {
    if (!active) return undefined
    historyRef.current = new Array(BAR_COUNT).fill(0)

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

      barRefs.current.forEach((bar, i) => {
        if (!bar) return
        const v = history[i]
        bar.style.transform = `scaleY(${(0.12 + v * 0.88).toFixed(3)})`
        bar.style.opacity = (0.35 + v * 0.65).toFixed(3)
      })

      frameRef.current = requestAnimationFrame(animate)
    }

    frameRef.current = requestAnimationFrame(animate)
    return () => {
      if (frameRef.current) cancelAnimationFrame(frameRef.current)
    }
  }, [active, getWaveform])

  if (!active) return null

  return (
    <div className="composer__waveform-bars" aria-hidden="true">
      {Array.from({ length: BAR_COUNT }).map((_, i) => (
        <span
          key={i}
          ref={(el) => {
            barRefs.current[i] = el
          }}
          className="composer__waveform-bar"
        />
      ))}
    </div>
  )
}
