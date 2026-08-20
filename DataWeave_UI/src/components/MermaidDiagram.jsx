import { useEffect, useId, useState } from 'react'
import mermaid from 'mermaid'

/**
 * Renders a Mermaid code block as an inline SVG diagram.
 *
 * Designed to survive streaming: while tokens are still arriving the fenced
 * block is incomplete and Mermaid.render throws, so we fall back to showing the
 * raw source until the syntax becomes valid. This keeps the chat from ever
 * crashing on a half-written diagram.
 *
 * Charts are sized and coloured from the app's own CSS theme variables so they
 * stay readable and follow light/dark theme switches, and xy-charts widen with
 * the number of categories (then scroll) so axis labels never collapse into an
 * unreadable blur.
 */
function cssVar(name, fallback) {
  const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim()
  return value || fallback
}

function isDarkTheme() {
  // Layout sets data-theme-mode to exactly "dark"/"light" for every theme,
  // whereas data-theme holds the specific theme name (e.g. "academic-dark"),
  // which wouldn't match a bare "dark"/"light" check for 4 of the 6 themes.
  const mode = document.documentElement.getAttribute('data-theme-mode')
  if (mode === 'light') return false
  if (mode === 'dark') return true
  return Boolean(window.matchMedia?.('(prefers-color-scheme: dark)')?.matches)
}

// Mid-luminance categorical palette that reads well on both light and dark
// backgrounds. The app accent leads so single-series charts match the brand.
const BASE_PALETTE = [
  '#4c78a8', '#59a14f', '#e1a54b', '#8a6bbf',
  '#d1495b', '#43938a', '#b07aa1', '#6b8e23',
]

// Count the x-axis categories in an xychart-beta block so we can give each one
// enough horizontal room. Matches:  x-axis [Jan, Feb, Mar]  or  x-axis "T" [..]
function xAxisCategoryCount(source) {
  const match = source.match(/x-axis[^\n[]*\[([^\]]*)\]/i)
  if (!match) return 0
  return match[1].split(',').filter((s) => s.trim().length > 0).length
}

function buildConfig(source) {
  const dark = isDarkTheme()
  const text = cssVar('--text-primary', dark ? '#ededed' : '#1a1a1a')
  const muted = cssVar('--text-muted', dark ? '#8e8e8e' : '#767676')
  const line = cssVar('--panel-border-strong', dark ? '#3a3a3a' : '#bdbdbd')
  // Use --accent (the saturated brand fill), NOT --accent-strong. The latter is
  // a high-luminance *emphasis* color meant for text/borders — on dark themes
  // it's near-white (e.g. aurora-dark #daf4ed), which washes bars out to an
  // unreadable pale blur. --accent is mid-luminance and reads on every theme.
  const accent = cssVar('--accent', cssVar('--accent-strong', '#d97757'))
  const palette = [accent, ...BASE_PALETTE].join(', ')

  const isXY = /^xychart-beta/i.test(source.trimStart())
  const categories = isXY ? xAxisCategoryCount(source) : 0
  // Give every category ~110px so bars are chunky and labels never overlap; the
  // container scrolls horizontally when the natural width exceeds the bubble.
  const width = isXY ? Math.max(900, categories * 110) : undefined

  return {
    isXY,
    config: {
      startOnLoad: false,
      securityLevel: 'strict',
      // Without this, a failed parse (broken syntax, a still-streaming block, or
      // a false-positive detection) makes mermaid inject a "Syntax error in
      // text" bomb graphic into <body> — outside our React tree, so it never
      // gets cleaned up and stays stuck at the bottom of the page. Suppressing
      // it means render() simply rejects and we fall back to the raw source.
      suppressErrorRendering: true,
      fontFamily: 'inherit',
      theme: 'base',
      themeVariables: {
        fontSize: '16px',
        xyChart: {
          backgroundColor: 'transparent',
          titleColor: text,
          xAxisLabelColor: muted,
          xAxisTitleColor: text,
          xAxisTickColor: line,
          xAxisLineColor: line,
          yAxisLabelColor: muted,
          yAxisTitleColor: text,
          yAxisTickColor: line,
          yAxisLineColor: line,
          plotColorPalette: palette,
        },
      },
      xyChart: {
        width,
        height: 560,
        titleFontSize: 22,
        xAxis: { labelFontSize: 16, titleFontSize: 16, labelPadding: 8 },
        yAxis: { labelFontSize: 15, titleFontSize: 16 },
        plotReservedSpacePercent: 60,
      },
    },
  }
}

// Light sanitizer for common model output mistakes that cause Mermaid parse
// errors. Applied before every render attempt so charts still work even when
// the LLM uses typographic or Unicode alternatives to ASCII syntax.
function sanitizeMermaid(source) {
  return source
    // Strip any leaked closing-fence line (e.g. ``` [1]) from the end of the
    // content. This happens when the LLM appends a citation to the closing
    // fence and the markdown parser includes it as code content because the
    // citation makes the line an invalid CommonMark closing fence.
    .replace(/\n`{3,}[^\n]*$/, '')
    // → (U+2192) and — followed by > (em-dash, U+2014) are both used instead
    // of mermaid's range arrow -->
    .replace(/→/g, '-->')
    .replace(/—>/g, '-->')
    // “ “ (U+201C/201D curly quotes) → straight quotes
    .replace(/[“”]/g, '”')
}

export default function MermaidDiagram({ code }) {
  const [svg, setSvg] = useState('')
  const [failed, setFailed] = useState(false)
  const [wide, setWide] = useState(false)
  // A stable, render-pure id that is also a valid CSS identifier for Mermaid.
  const renderId = `mermaid-${useId().replace(/[^a-zA-Z0-9]/g, '')}`

  useEffect(() => {
    let cancelled = false
    const source = sanitizeMermaid((code || '').trim())
    if (!source) return undefined

    const { isXY, config } = buildConfig(source)
    mermaid.initialize(config)

    mermaid
      .render(renderId, source)
      .then(({ svg: rendered }) => {
        if (cancelled) return
        setSvg(rendered)
        setWide(isXY)
        setFailed(false)
      })
      .catch(() => {
        // Invalid or still-streaming syntax — fall back to the raw source.
        // Belt-and-suspenders: remove any orphan node mermaid may have appended
        // to <body> before throwing, so no stray error graphic leaks onto the
        // page (suppressErrorRendering above should already prevent this).
        document.getElementById(renderId)?.remove()
        document.getElementById(`d${renderId}`)?.remove()
        if (cancelled) return
        setFailed(true)
      })

    return () => {
      cancelled = true
    }
  }, [code, renderId])

  const hasSource = Boolean((code || '').trim())

  if (hasSource && svg && !failed) {
    return (
      <div
        className={wide ? 'mermaid-diagram mermaid-diagram--wide' : 'mermaid-diagram'}
        role="img"
        // Mermaid output is sanitized (securityLevel: 'strict').
        dangerouslySetInnerHTML={{ __html: svg }}
      />
    )
  }

  if (!hasSource) return null

  return (
    <pre className="mermaid-fallback">
      <code>{code}</code>
    </pre>
  )
}
