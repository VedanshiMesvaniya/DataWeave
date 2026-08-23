import { useCallback, useEffect, useState } from 'react'
import { Quote } from 'lucide-react'

/**
 * Select-to-reply, the same pattern ChatGPT/Claude use: select any text
 * inside a message and a small "Reply" pill appears above the selection.
 * Clicking it hands the exact selected text to `onReply`, which
 * (in Chat.jsx) quotes it into the composer as a markdown blockquote ahead
 * of whatever the person types next — so the model sees precisely which
 * part of the conversation the follow-up question is about, the same way
 * it would if the person had retyped the passage themselves.
 *
 * Scoped to `containerRef` (the scrollable message list) — selections
 * started elsewhere (e.g. inside the composer) are ignored, and the popup
 * is positioned as a child of that same scrollable element so it scrolls
 * naturally with the content instead of drifting from the selection.
 */
export default function SelectionReplyPopup({ containerRef, onReply }) {
  const [popup, setPopup] = useState(null) // { top, left, text } | null

  const updateFromSelection = useCallback(() => {
    const selection = window.getSelection()
    if (!selection || selection.isCollapsed || selection.rangeCount === 0) {
      setPopup(null)
      return
    }

    const text = selection.toString().trim()
    const container = containerRef.current
    const anchorNode = selection.anchorNode
    if (!text || !container || !anchorNode || !container.contains(anchorNode)) {
      setPopup(null)
      return
    }

    const range = selection.getRangeAt(0)
    const rect = range.getBoundingClientRect()
    if (rect.width === 0 && rect.height === 0) {
      setPopup(null)
      return
    }

    const containerRect = container.getBoundingClientRect()
    // Convert the viewport-relative selection rect into a position measured
    // from the top of the scrollable content (not just the visible window),
    // since the popup renders as an absolutely-positioned child of this same
    // scrolling container — adding scrollTop back keeps it pinned to the
    // selection as the person scrolls, rather than jumping on next render.
    setPopup({
      top: rect.top - containerRect.top + container.scrollTop,
      left: rect.left - containerRect.left + rect.width / 2,
      text,
    })
  }, [containerRef])

  useEffect(() => {
    document.addEventListener('selectionchange', updateFromSelection)
    return () => document.removeEventListener('selectionchange', updateFromSelection)
  }, [updateFromSelection])

  if (!popup) return null

  return (
    <button
      type="button"
      className="selection-reply"
      style={{ top: `${popup.top}px`, left: `${popup.left}px` }}
      // The browser's own selection collapses on mousedown, before a click
      // handler would fire — capture the quoted text now, on mousedown, and
      // suppress the default so the selection survives long enough to read.
      onMouseDown={(event) => {
        event.preventDefault()
        onReply(popup.text)
        window.getSelection()?.removeAllRanges()
        setPopup(null)
      }}
    >
      <Quote size={13} />
      <span>Reply</span>
    </button>
  )
}
