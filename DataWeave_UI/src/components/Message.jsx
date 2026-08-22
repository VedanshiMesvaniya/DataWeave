import { motion } from 'framer-motion'
import { useEffect, useRef, useState } from 'react'
import {
  Check,
  ChevronLeft,
  ChevronRight,
  Copy,
  PencilLine,
  RefreshCw,
  ThumbsDown,
  ThumbsUp,
  X,
} from 'lucide-react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import rehypeHighlight from 'rehype-highlight'
import clsx from 'clsx'
import TextareaAutosize from 'react-textarea-autosize'
import { useAppStore } from '../store/store.js'
import MermaidDiagram from './MermaidDiagram.jsx'
import IngestionCard from './IngestionCard.jsx'
import ThinkingTrace from './ThinkingTrace.jsx'
import TokenUsage from './TokenUsage.jsx'
import rehypeCitations from './rehypeCitations.js'

/** Recursively flatten a react-markdown children tree back into plain text.
 * rehype-highlight can split code into nested <span> tokens, so a simple
 * String() is not enough — we walk the tree and concatenate the text. */
function nodeText(node) {
  if (node == null || node === false) return ''
  if (typeof node === 'string' || typeof node === 'number') return String(node)
  if (Array.isArray(node)) return node.map(nodeText).join('')
  if (typeof node === 'object' && node.props) return nodeText(node.props.children)
  return ''
}

// First-line directives that unambiguously mark a Mermaid diagram, so we can
// still render when the model fences the block as ```xychart / plain ``` /
// anything other than ```mermaid.
const MERMAID_DIRECTIVE =
  /^(?:%%\{[^]*?\}%%\s*)?(?:xychart-beta|pie\b|flowchart\b|graph\b|sequenceDiagram\b|timeline\b|gantt\b|classDiagram\b|stateDiagram(?:-v2)?\b|erDiagram\b|journey\b|mindmap\b|quadrantChart\b)/

// When a model fences a block as ```xychart-beta (not ```mermaid), the language
// tag becomes the className — but the content body no longer has the directive on
// its first line, so MERMAID_DIRECTIVE can't catch it. This regex matches any
// mermaid chart-type language class so we can prepend the directive ourselves.
const MERMAID_LANG_RE =
  /^language-(xychart-beta|xychart|pie|flowchart|graph|sequenceDiagram|timeline|gantt|classDiagram|stateDiagram(?:-v2)?|erDiagram|journey|mindmap|quadrantChart)$/i

/**
 * Pull the source out of a fenced code block if it's a Mermaid diagram.
 * Handles three fence styles the model might produce:
 *   1. ```mermaid   — content already starts with the chart directive.
 *   2. ```xychart-beta (etc.) — directive is the language tag; we prepend it.
 *   3. ``` (unlabelled) — directive is the first line of content.
 */
function mermaidSource(children) {
  const child = Array.isArray(children) ? children[0] : children
  if (!child?.props) return null
  const className = child.props.className || ''
  const source = nodeText(child.props.children).replace(/\n$/, '')
  // Case 1: ```mermaid — content already contains the directive header.
  if (/\bmermaid\b/.test(className)) return source
  // Case 2: ```xychart-beta etc. — language tag IS the directive; prepend it.
  const lang = className.match(MERMAID_LANG_RE)?.[1]
  if (lang) return `${lang}\n${source}`
  // Case 3: unlabelled/mis-labelled fence whose content starts with a directive.
  if (MERMAID_DIRECTIVE.test(source.trimStart())) return source
  return null
}

// CommonMark requires closing code fences to have only spaces after the
// backticks. LLMs often append citation markers ([1]) directly after the
// closing backticks, which prevents the fence from closing the block and leaks
// the fence line into the code content. Move those citations to the next line
// so the parser sees a valid closing fence.
function normalizeClosingFences(text) {
  return text.replace(/^(`{3,}|~{3,})[ \t]*(\[\d+(?:[,\s]*\d+)*\])/gm, '$1\n$2')
}

// Custom renderers for assistant markdown: mermaid code blocks become diagrams,
// everything else falls through to the default <pre>.
const markdownComponents = {
  pre(props) {
    const { children, ...rest } = props
    const source = mermaidSource(children)
    if (source !== null) return <MermaidDiagram code={source} />
    return <pre {...rest}>{children}</pre>
  },
}

function useTypewriterText(text, enabled) {
  const [displayedText, setDisplayedText] = useState(enabled ? '' : text)

  useEffect(() => {
    if (!enabled) {
      // This hook intentionally syncs local animation state with incoming props.
      setDisplayedText(text)
      return undefined
    }

    const reducedMotion =
      typeof window !== 'undefined' &&
      window.matchMedia?.('(prefers-reduced-motion: reduce)')?.matches

    if (reducedMotion) {
      // This hook intentionally syncs local animation state with incoming props.
      setDisplayedText(text)
      return undefined
    }

    setDisplayedText('')

    let index = 0
    const interval = window.setInterval(() => {
      index += 1
      setDisplayedText(text.slice(0, index))

      if (index >= text.length) {
        window.clearInterval(interval)
      }
    }, 18)

    return () => window.clearInterval(interval)
  }, [enabled, text])

  return displayedText
}

export default function Message({ message, index = 0, chatId, isLast = false }) {
  const markMessageAsSeen = useAppStore((state) => state.markMessageAsSeen)
  const setMessageFeedback = useAppStore((state) => state.setMessageFeedback)
  const regenerateMessage = useAppStore((state) => state.regenerateMessage)
  const setMessageVersion = useAppStore((state) => state.setMessageVersion)
  const editMessage = useAppStore((state) => state.editMessage)
  const loading = useAppStore((state) => state.loading)
  const isAssistant = message.role === 'assistant'
  const isLoading = message.status === 'loading'
  const isStreaming = message.status === 'streaming'
  const [copied, setCopied] = useState(false)
  const [isEditing, setIsEditing] = useState(false)
  const [draft, setDraft] = useState(message.content || '')
  
  const submitFeedbackComment = useAppStore((state) => state.submitFeedbackComment)
  const [feedbackOpen, setFeedbackOpen] = useState(false)
  const [feedbackText, setFeedbackText] = useState('')
  
  const editRef = useRef(null)
  const copyTimerRef = useRef(null)
  const typedContent = useTypewriterText(
    message.content || '',
    isAssistant && !isLoading && !!message.isNew,
  )
  // Show the blinking cursor both while the typewriter fallback is actively
  // typing AND while real tokens are streaming in live from the backend —
  // the latter never touches useTypewriterText's animation path (isNew is
  // never set for streamed completions), so it needs its own indicator.
  const isTyping =
    (isAssistant && !isLoading && typedContent.length < (message.content || '').length) || isStreaming
  const feedback = message.feedback || null
  const canRegenerate = isAssistant && !isLoading && !isStreaming && isLast
  const canEdit = !isAssistant && !loading
  const versions = message.versions || []
  const activeVersion = message.activeVersion ?? Math.max(0, versions.length - 1)
  const hasVersions = versions.length > 1 && !isLoading && !isStreaming

  useEffect(() => {
    if (!isEditing) {
      setDraft(message.content || '')
    }
  }, [isEditing, message.content])

  useEffect(() => {
    if (!isEditing) return undefined
    const frame = window.requestAnimationFrame(() => {
      editRef.current?.focus()
      editRef.current?.setSelectionRange?.(draft.length, draft.length)
    })

    return () => window.cancelAnimationFrame(frame)
  }, [draft.length, isEditing])

  useEffect(
    () => () => {
      if (copyTimerRef.current) window.clearTimeout(copyTimerRef.current)
    },
    [],
  )

  useEffect(() => {
    if (!chatId || !message.isNew || isTyping) return
    markMessageAsSeen(chatId, message.id)
  }, [chatId, isTyping, markMessageAsSeen, message.id, message.isNew])

  const handleCopy = async () => {
    if (!message.content) return

    try {
      await navigator.clipboard.writeText(message.content)
      setCopied(true)
      if (copyTimerRef.current) window.clearTimeout(copyTimerRef.current)
      copyTimerRef.current = window.setTimeout(() => setCopied(false), 1500)
    } catch {
      // Clipboard access can fail outside a secure context.
    }
  }

  const handleFeedback = (value) => {
    setMessageFeedback(chatId, message.id, value)
    setFeedbackOpen(true)
    setFeedbackText('')
  }

  const handleFeedbackSubmit = () => {
    if (feedbackText.trim()) {
      submitFeedbackComment(chatId, message.id, feedbackText.trim())
    }
    setFeedbackOpen(false)
    setFeedbackText('')
  }

  const handleSaveEdit = async () => {
    if (!canEdit || !chatId) return

    const nextContent = draft.trim()
    if (!nextContent || nextContent === (message.content || '').trim()) {
      setIsEditing(false)
      setDraft(message.content || '')
      return
    }

    setIsEditing(false)
    await editMessage(chatId, message.id, nextContent)
  }

  const handleCancelEdit = () => {
    setDraft(message.content || '')
    setIsEditing(false)
  }

  if (message.kind === 'ingestion') {
    return (
      <motion.div
        className="message message--assistant message--ingestion"
        initial={{ opacity: 0, y: 12 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.24, delay: index * 0.03 }}
      >
        <IngestionCard message={message} />
      </motion.div>
    )
  }

  return (
    <motion.div
      className={clsx('message', `message--${message.role}`, {
        'message--loading': isLoading,
      })}
      initial={{ opacity: 0, y: 12 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.24, delay: index * 0.03 }}
    >
      {isLoading ? (
        <div className="message__assistant message__assistant--loading" aria-live="polite">
          {message.thinking?.length ? (
            <ThinkingTrace steps={message.thinking} streaming />
          ) : (
            <span className="message__typing">
              <span className="loader__dot" />
              <span className="loader__dot" />
              <span className="loader__dot" />
            </span>
          )}
        </div>
      ) : isAssistant ? (
        <div className="message__content">
          {message.thinking?.length ? (
            <ThinkingTrace steps={message.thinking} streaming={isStreaming} />
          ) : null}
          <div className="message__assistant markdown">
            <ReactMarkdown
              remarkPlugins={[remarkGfm]}
              rehypePlugins={[[rehypeHighlight, { ignoreMissing: true }], rehypeCitations]}
              components={markdownComponents}
            >
              {normalizeClosingFences(typedContent)}
            </ReactMarkdown>
            {isTyping ? <span className="typing-cursor" aria-hidden="true" /> : null}
          </div>
          {hasVersions ? (
            <div className="message__versions" role="group" aria-label="Answer versions">
              <button
                type="button"
                className="message__version-nav"
                onClick={() => setMessageVersion(chatId, message.id, activeVersion - 1)}
                disabled={activeVersion <= 0}
                aria-label="Previous version"
              >
                <ChevronLeft size={14} />
              </button>
              <span className="message__version-count" aria-live="polite">
                {activeVersion + 1}/{versions.length}
              </span>
              <button
                type="button"
                className="message__version-nav"
                onClick={() => setMessageVersion(chatId, message.id, activeVersion + 1)}
                disabled={activeVersion >= versions.length - 1}
                aria-label="Next version"
              >
                <ChevronRight size={14} />
              </button>
            </div>
          ) : null}
          <div className="message__actions" aria-label="Assistant actions">
            <button
              type="button"
              className={clsx('message__action', copied && 'message__action--active')}
              onClick={handleCopy}
              aria-label="Copy message"
            >
              {copied ? <Check size={14} /> : <Copy size={14} />}
            </button>
            <button
              type="button"
              className={clsx('message__action', feedback === 'up' && 'message__action--active')}
              onClick={() => handleFeedback('up')}
              aria-label="Thumbs up"
            >
              <ThumbsUp size={14} />
            </button>
            <button
              type="button"
              className={clsx('message__action', feedback === 'down' && 'message__action--active')}
              onClick={() => handleFeedback('down')}
              aria-label="Thumbs down"
            >
              <ThumbsDown size={14} />
            </button>
            {canRegenerate ? (
              <button
                type="button"
                className="message__action"
                onClick={() => regenerateMessage(chatId, message.id)}
                aria-label="Regenerate reply"
                disabled={loading}
              >
                <RefreshCw size={14} />
              </button>
            ) : null}
          </div>
          
          {feedbackOpen && (
            <motion.div
              className="message__feedback"
              initial={{ opacity: 0, height: 0 }}
              animate={{ opacity: 1, height: 'auto' }}
            >
              <input
                className="message__feedback-input"
                placeholder="What was wrong? (optional)"
                value={feedbackText}
                onChange={(e) => setFeedbackText(e.target.value)}
                onKeyDown={(e) => e.key === 'Enter' && handleFeedbackSubmit()}
              />
              <button className="message__feedback-submit" onClick={handleFeedbackSubmit}>
                Send
              </button>
              <button className="message__feedback-skip" onClick={() => setFeedbackOpen(false)}>
                Skip
              </button>
            </motion.div>
          )}

          {/* Token cost of this answer — a collapsible footer, mirroring the
              thinking trace at the top. Self-hides when no usage was captured. */}
          {!isStreaming ? <TokenUsage usage={message.usage} /> : null}
        </div>
      ) : (
        <div className="message__content">
          {isEditing ? (
            <div className="message__edit-shell">
              <TextareaAutosize
                ref={editRef}
                className="message__edit"
                value={draft}
                onChange={(event) => setDraft(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === 'Escape') {
                    event.preventDefault()
                    handleCancelEdit()
                    return
                  }

                  if (event.key !== 'Enter' || event.shiftKey) return
                  event.preventDefault()
                  handleSaveEdit()
                }}
                minRows={3}
                maxRows={10}
              />
              <div className="message__edit-actions">
                <button type="button" className="secondary-button" onClick={handleCancelEdit}>
                  <X size={14} />
                  <span>Cancel</span>
                </button>
                <button
                  type="button"
                  className="primary-button"
                  onClick={handleSaveEdit}
                  disabled={!draft.trim() || draft.trim() === (message.content || '').trim()}
                >
                  <PencilLine size={14} />
                  <span>Save</span>
                </button>
              </div>
            </div>
          ) : (
            <>
              <div className="message__bubble markdown">
                <ReactMarkdown
                  remarkPlugins={[remarkGfm]}
                  rehypePlugins={[[rehypeHighlight, { ignoreMissing: true }], rehypeCitations]}
                >
                  {message.content}
                </ReactMarkdown>
              </div>
              <div className="message__actions message__actions--user">
                <button
                  type="button"
                  className={clsx('message__action', copied && 'message__action--active')}
                  onClick={handleCopy}
                  aria-label="Copy message"
                >
                  {copied ? <Check size={14} /> : <Copy size={14} />}
                </button>
                <button
                  type="button"
                  className="message__action"
                  onClick={() => setIsEditing(true)}
                  aria-label="Edit message"
                  disabled={!canEdit}
                >
                  <PencilLine size={14} />
                </button>
              </div>
            </>
          )}
        </div>
      )}
    </motion.div>
  )
}
