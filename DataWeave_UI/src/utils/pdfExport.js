import dayjs from 'dayjs'
import { marked } from 'marked'
import mermaid from 'mermaid'

/**
 * PDF export via the browser's own print engine (a hidden iframe + print()).
 *
 * This renders Markdown to real HTML — headings, tables, lists, code — and
 * turns Mermaid code blocks into inline SVG charts, so the output is properly
 * formatted and includes graphs, unlike the old raw-text dump. The print
 * engine handles pagination, page breaks, and fonts far more reliably than
 * canvas-based approaches.
 *
 * Two modes share this pipeline:
 *   - Chat transcript: the conversation, cleanly formatted.
 *   - Professional document: an LLM-restructured report (built server-side)
 *     that may add charts the raw chat never had.
 */

marked.setOptions({ gfm: true, breaks: false })

// First-line directives that mark a Mermaid diagram even without a ```mermaid tag.
const MERMAID_DIRECTIVE =
  /^(?:%%\{[^]*?\}%%\s*)?(?:xychart-beta|pie\b|flowchart\b|graph\b|sequenceDiagram\b|timeline\b|gantt\b|classDiagram\b|stateDiagram(?:-v2)?\b|erDiagram\b|journey\b|mindmap\b|quadrantChart\b)/

function escapeHtml(text) {
  const div = document.createElement('div')
  div.textContent = text ?? ''
  return div.innerHTML
}

export function safeFilename(title) {
  const cleaned = (title || 'document')
    .trim()
    .split('')
    .filter((char) => char.charCodeAt(0) >= 32 && !'<>:"/\\|?*'.includes(char))
    .join('')
    .replace(/\s+/g, ' ')
    .slice(0, 80)
  return cleaned || 'document'
}

/**
 * Parse Markdown to HTML, then replace every Mermaid code block with a rendered
 * inline SVG. Mermaid renders against the main document; the resulting SVG
 * string is self-contained, so it drops cleanly into the print iframe.
 */
async function markdownToHtmlWithCharts(markdownText) {
  const host = document.createElement('div')
  host.innerHTML = marked.parse(markdownText || '')

  mermaid.initialize({
    startOnLoad: false,
    securityLevel: 'strict',
    suppressErrorRendering: true,
    theme: 'neutral',
    fontFamily: 'Helvetica, Arial, sans-serif',
  })

  const codeBlocks = Array.from(host.querySelectorAll('pre > code'))
  let counter = 0
  for (const code of codeBlocks) {
    const className = code.getAttribute('class') || ''
    const source = (code.textContent || '').replace(/\n$/, '')
    const isMermaid = /\bmermaid\b/.test(className) || MERMAID_DIRECTIVE.test(source.trimStart())
    if (!isMermaid) continue

    try {
      const { svg } = await mermaid.render(`pdf-chart-${Date.now()}-${counter++}`, source.trim())
      const figure = document.createElement('figure')
      figure.className = 'pdf-chart'
      figure.innerHTML = svg
      code.closest('pre').replaceWith(figure)
    } catch {
      // Unparseable — leave the raw code block as-is rather than break export.
    }
  }

  return host.innerHTML
}

const PRINT_STYLES = `
  @page { margin: 2cm; }
  * { box-sizing: border-box; }
  body {
    font-family: 'Georgia', 'Times New Roman', serif;
    color: #1a1a1a;
    line-height: 1.6;
    font-size: 12pt;
    margin: 0;
  }
  .doc-header { border-bottom: 2px solid #333; padding-bottom: 12px; margin-bottom: 24px; }
  .doc-title { font-size: 22pt; font-weight: 700; margin: 0 0 6px; }
  .doc-subtitle { font-size: 10pt; color: #666; margin: 0; }
  h1, h2, h3 { font-family: 'Helvetica', Arial, sans-serif; line-height: 1.25; page-break-after: avoid; }
  h1 { font-size: 18pt; margin: 24px 0 10px; }
  h2 { font-size: 15pt; margin: 20px 0 8px; border-bottom: 1px solid #ddd; padding-bottom: 4px; }
  h3 { font-size: 13pt; margin: 16px 0 6px; }
  p { margin: 0 0 10px; }
  ul, ol { margin: 0 0 10px 22px; }
  li { margin: 2px 0; }
  code { font-family: 'Menlo', monospace; font-size: 10pt; background: #f3f3f3; padding: 1px 4px; border-radius: 3px; }
  pre { background: #f6f6f6; border: 1px solid #e2e2e2; border-radius: 6px; padding: 10px; overflow-x: auto; page-break-inside: avoid; }
  pre code { background: none; padding: 0; }
  table { border-collapse: collapse; width: 100%; margin: 12px 0; font-size: 10.5pt; page-break-inside: avoid; }
  th, td { border: 1px solid #ccc; padding: 6px 9px; text-align: left; }
  th { background: #f0f0f0; font-weight: 600; }
  blockquote { border-left: 3px solid #ccc; margin: 10px 0; padding: 2px 14px; color: #555; }
  sup { color: #777; font-size: 0.75em; }
  .pdf-chart { margin: 16px 0; text-align: center; page-break-inside: avoid; }
  .pdf-chart svg { max-width: 100%; height: auto; }
  .msg { margin: 0 0 18px; page-break-inside: avoid; }
  .msg-role { font-family: 'Helvetica', Arial, sans-serif; font-weight: 700; font-size: 10.5pt; color: #444; margin-bottom: 4px; }
  .msg-role--assistant { color: #b45309; }
  hr { border: none; border-top: 1px solid #e0e0e0; margin: 18px 0; }
`

/**
 * Build the print document, wait for layout, and open the print dialog
 * (where the user chooses "Save as PDF"). Uses a hidden iframe so it never
 * disturbs the app.
 */
async function printHtml(title, bodyHtml) {
  const doc = `<!doctype html>
<html>
  <head>
    <meta charset="utf-8" />
    <title>${escapeHtml(title)}</title>
    <style>${PRINT_STYLES}</style>
  </head>
  <body>${bodyHtml}</body>
</html>`

  const iframe = document.createElement('iframe')
  iframe.setAttribute('aria-hidden', 'true')
  iframe.style.cssText = 'position:fixed;right:0;bottom:0;width:0;height:0;border:0;'
  document.body.appendChild(iframe)

  await new Promise((resolve) => {
    iframe.onload = resolve
    const idoc = iframe.contentDocument
    idoc.open()
    idoc.write(doc)
    idoc.close()
    // Some browsers don't fire onload for document.write; fall back on a timer.
    setTimeout(resolve, 400)
  })

  // Give layout/SVG a beat to settle before printing.
  await new Promise((r) => setTimeout(r, 250))
  iframe.contentWindow.focus()
  iframe.contentWindow.print()

  // Clean up after the print dialog has had time to capture the document.
  setTimeout(() => iframe.remove(), 1500)
}

/** Export the raw conversation as a formatted PDF (charts included). */
export async function exportChatTranscript(chat, messages = []) {
  const printable = messages.filter(
    (m) => m.status !== 'loading' && m.kind !== 'ingestion' && (m.content || '').trim(),
  )

  const parts = [
    `<div class="doc-header">`,
    `<h1 class="doc-title">${escapeHtml(chat?.title || 'Chat')}</h1>`,
    `<p class="doc-subtitle">Chat transcript · Exported ${escapeHtml(dayjs().format('MMM D, YYYY h:mm A'))}</p>`,
    `</div>`,
  ]

  for (const message of printable) {
    const role = message.role === 'user' ? 'You' : 'Assistant'
    const roleClass = message.role === 'user' ? '' : 'msg-role--assistant'
    const body = await markdownToHtmlWithCharts(message.content)
    parts.push(
      `<div class="msg"><div class="msg-role ${roleClass}">${role}</div>${body}</div>`,
    )
  }

  if (!printable.length) parts.push('<p>No messages to export.</p>')

  await printHtml(chat?.title || 'Chat', parts.join('\n'))
}

/** Export an already-built professional-document Markdown as a formatted PDF. */
export async function exportProfessionalDocument({ title, markdown }) {
  const body = await markdownToHtmlWithCharts(markdown || '')
  const html = [
    `<div class="doc-header">`,
    `<p class="doc-subtitle">Generated document · ${escapeHtml(dayjs().format('MMM D, YYYY'))}</p>`,
    `</div>`,
    body,
  ].join('\n')
  await printHtml(title || 'Document', html)
}
