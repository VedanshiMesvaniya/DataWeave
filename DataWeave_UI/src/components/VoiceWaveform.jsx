import { useEffect, useRef } from 'react'

const BAR_COUNT = 5

/**
 * A small live equalizer shown next to the mic button while recording —
 * reacts to actual mic input (via `getLevels`), not a canned animation.
 *
 * Bar heights are driven with direct DOM style writes inside a
 * requestAnimationFrame loop rather than React state, since audio levels can
 * update 30-60x/sec and that shouldn't trigger a React re-render on every
 * frame — this component is the only thing that repaints.
 */
export default function VoiceWaveform({ getLevels, active }) {
  const barRefs = useRef([])
  const frameRef = useRef(null)

  useEffect(() => {
    if (!active) return undefined

    const animate = () => {
      const levels = getLevels?.() || []
      barRefs.current.forEach((bar, i) => {
        if (!bar) return
        const level = levels[i] ?? 0
        // Floor at a small resting height so bars read as "listening" even
        // during brief silence, not as flat-lined/broken.
        const scale = 0.22 + level * 0.78
        bar.style.transform = `scaleY(${scale})`
      })
      frameRef.current = requestAnimationFrame(animate)
    }

    frameRef.current = requestAnimationFrame(animate)
    return () => {
      if (frameRef.current) cancelAnimationFrame(frameRef.current)
    }
  }, [active, getLevels])

  if (!active) return null

  return (
    <div className="composer__waveform" aria-hidden="true">
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
