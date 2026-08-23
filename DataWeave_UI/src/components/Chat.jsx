import { AnimatePresence, motion } from 'framer-motion'
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { Download, FileText, Loader2, Sparkles } from 'lucide-react'
import { toast } from 'sonner'
import { useAppStore } from '../store/store.js'
import InputBox from './InputBox.jsx'
import Loader from './Loader.jsx'
import Message from './Message.jsx'
import ProviderStatus from './ProviderStatus.jsx'
import { generateChatDocument } from '../services/api.js'
import { exportChatTranscript, exportProfessionalDocument } from '../utils/pdfExport.js'
import { useVoiceRecorder } from '../utils/useVoiceRecorder.js'

export default function Chat() {
  const activeChatId = useAppStore((state) => state.activeChatId)
  const messagesByChatId = useAppStore((state) => state.messagesByChatId)
  const sendPrompt = useAppStore((state) => state.sendPrompt)
  const stopGeneration = useAppStore((state) => state.stopGeneration)
  const activeRequest = useAppStore((state) => state.activeRequest)
  const chats = useAppStore((state) => state.chats)
  const loading = useAppStore((state) => state.loading)
  const [value, setValue] = useState('')
  const inputRef = useRef(null)
  const { status: micStatus, toggleRecording, getLevels: getMicLevels } = useVoiceRecorder({
    onTranscript: (text) => {
      setValue((current) => (current.trim() ? `${current.trim()} ${text}` : text))
      inputRef.current?.focus()
    },
    onError: (message) => toast.error(message),
  })
  const bottomRef = useRef(null)
  const isGenerating = Boolean(activeRequest)

  const messages = useMemo(
    () => messagesByChatId[activeChatId] || [],
    [activeChatId, messagesByChatId],
  )
  const lastMessageId = messages[messages.length - 1]?.id
  const hasMessages = messages.length > 0

  const activeChat = chats.find((chat) => chat.id === activeChatId)
  const exportableMessages = messages.filter(
    (message) => message != null && message.status !== 'loading' && message.kind !== 'ingestion',
  )
  const canExport = Boolean(activeChat) && exportableMessages.length > 0

  const [menuOpen, setMenuOpen] = useState(false)
  const [busy, setBusy] = useState(false)
  const menuRef = useRef(null)

  useEffect(() => {
    if (!menuOpen) return undefined
    const onClick = (e) => {
      if (menuRef.current && !menuRef.current.contains(e.target)) setMenuOpen(false)
    }
    window.addEventListener('mousedown', onClick)
    return () => window.removeEventListener('mousedown', onClick)
  }, [menuOpen])

  const handleTranscript = async () => {
    setMenuOpen(false)
    if (!canExport) return
    try {
      await exportChatTranscript(activeChat, exportableMessages)
    } catch (error) {
      console.error(error)
      toast.error('Could not export the transcript.')
    }
  }

  const handleProfessional = async () => {
    setMenuOpen(false)
    if (!canExport || busy) return
    setBusy(true)
    toast.info('Building your professional document…')
    try {
      const { markdown, title: docTitle } = await generateChatDocument(activeChatId)
      await exportProfessionalDocument({ title: docTitle, markdown })
    } catch (error) {
      console.error(error)
      toast.error('Could not generate the document. Please try again.')
    } finally {
      setBusy(false)
    }
  }

  useEffect(() => {
    if (!chats.length) {
      toast.info('Waiting for demo chat data.')
    }
  }, [chats.length])

  useEffect(() => {
    inputRef.current?.focus()
  }, [activeChatId])

  useLayoutEffect(() => {
    const container = document.querySelector('.message-stream')
    if (!container) return undefined

    const frame = window.requestAnimationFrame(() => {
      container.scrollTo({
        top: container.scrollHeight,
        behavior: 'smooth',
      })
    })

    return () => window.cancelAnimationFrame(frame)
  }, [activeChatId, lastMessageId, loading])

  return (
    <section className="chat-panel" data-has-messages={hasMessages ? 'true' : 'false'}>
      <div className="chat-panel__head">
        <p className="chat-panel__head-title">Chat View</p>
        <div className="header__actions" ref={menuRef}>
          <button
            className="icon-button"
            type="button"
            aria-label="Export as PDF"
            aria-haspopup="menu"
            aria-expanded={menuOpen}
            onClick={() => setMenuOpen((v) => !v)}
            disabled={!canExport || busy}
          >
            {busy ? <Loader2 size={18} className="spin" /> : <Download size={18} />}
          </button>

          {menuOpen ? (
            <div className="export-menu" role="menu">
              <button type="button" className="export-menu__item" role="menuitem" onClick={handleTranscript}>
                <FileText size={16} />
                <span>
                  <strong>Chat transcript</strong>
                  <em>The conversation, formatted with charts</em>
                </span>
              </button>
              <button type="button" className="export-menu__item" role="menuitem" onClick={handleProfessional}>
                <Sparkles size={16} />
                <span>
                  <strong>Professional document</strong>
                  <em>A polished report generated from this chat, charts added</em>
                </span>
              </button>
            </div>
          ) : null}
        </div>
      </div>

      <div className="message-stream scrollbar-auto">
        <div className="chat-panel__inner">
          <AnimatePresence mode="popLayout">
            {messages.length ? (
              messages.map((message, index) => (
                <Message
                  key={message.id}
                  message={message}
                  index={index}
                  chatId={activeChatId}
                  isLast={index === messages.length - 1}
                />
              ))
            ) : (
              <motion.div
                key="empty"
                className="hero"
                initial={{ opacity: 0, y: 10 }}
                animate={{ opacity: 1, y: 0 }}
                exit={{ opacity: 0, y: -10 }}
              >
                <p className="hero__eyebrow">Contextual Intelligence</p>
                <h2 className="hero__title">
                  How can I help <em>you</em> today?
                </h2>
              </motion.div>
            )}
          </AnimatePresence>

          {loading && !isGenerating ? <Loader /> : null}
          <div ref={bottomRef} aria-hidden="true" />
        </div>
      </div>

      <div className="composer">
        <InputBox
          ref={inputRef}
          value={value}
          onChange={setValue}
          onSubmit={async () => {
            if (isGenerating) return
            const prompt = value.trim()
            if (!prompt) return
            setValue('')
            await sendPrompt(prompt)
            inputRef.current?.focus()
          }}
          onStop={() => {
            stopGeneration()
            inputRef.current?.focus()
          }}
          loading={isGenerating}
          disabled={isGenerating}
          footer={<ProviderStatus />}
          micStatus={micStatus}
          onMicClick={toggleRecording}
          getMicLevels={getMicLevels}
        />
      </div>
    </section>
  )
}
