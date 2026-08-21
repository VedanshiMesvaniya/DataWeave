import { create } from 'zustand'
import { toast } from 'sonner'
import {
  createChat,
  deleteChat as deleteChatApi,
  generateChatTitle,
  setMessageFeedbackApi,
  submitFeedbackComment,
  getChats,
  getDocuments,
  getMessages,
  getOverview,
  getProviders,
  getProviderUsage,
  getSettings,
  persistIngestionCard,
  renameChat as renameChatApi,
  saveSettings,
  scanIngestFolder,
  sendMessage,
  sendMessageStream,
  uploadDocument,
  uploadDocumentStream,
  replaceDocumentStream,
  deleteDocument as deleteDocumentApi,
} from '../services/api.js'

// Pluralization helper for the inbox-scan popup ("1 file" vs "2 files").
const plural = (n) => (n === 1 ? '' : 's')

// The 10 ingestion stages, mirrored from the backend pipeline
// (_STAGE_LABELS in src/pipeline/ingestion.py).
const INGESTION_STAGES = [
  'File detection',
  'Document classification',
  'Content parsing',
  'OCR (scanned pages)',
  'Layout analysis',
  'Table extraction',
  'Visual analysis',
  'Chunking',
  'Embedding',
  'Storing in vector DB',
]

function normalizeList(value, fallback = []) {
  if (Array.isArray(value)) return value
  if (Array.isArray(value?.data)) return value.data
  if (Array.isArray(value?.items)) return value.items
  if (Array.isArray(value?.chats)) return value.chats
  if (Array.isArray(value?.documents)) return value.documents
  return fallback
}

const demoChats = [
  { id: 'chat-1', title: 'Project summary', updatedAt: new Date().toISOString() },
  { id: 'chat-2', title: 'Document Q&A', updatedAt: new Date().toISOString() },
]

let requestSequence = 0

function readStoredBoolean(key, fallback = false) {
  try {
    const raw = localStorage.getItem(key)
    if (raw === null) return fallback
    return JSON.parse(raw)
  } catch {
    return fallback
  }
}

function writeStoredBoolean(key, value) {
  try {
    localStorage.setItem(key, JSON.stringify(Boolean(value)))
  } catch {
    // Ignore storage issues and keep the in-memory state working.
  }
}

function buildUntitledChatTitle(prompt) {
  const trimmed = prompt.trim()
  if (trimmed.length <= 48) return trimmed
  return `${trimmed.slice(0, 45).trimEnd()}...`
}

function touchChat(chats, chatId) {
  const updatedAt = new Date().toISOString()
  return chats.map((chat) => (chat.id === chatId ? { ...chat, updatedAt } : chat))
}

function createLoadingAssistantMessage(requestId) {
  return {
    id: `assistant-pending-${requestId}-${Date.now()}`,
    role: 'assistant',
    content: '',
    createdAt: new Date().toISOString(),
    status: 'loading',
  }
}

// A single regenerated answer, archived so the user can page between versions.
function versionSnapshot(source) {
  return {
    content: source.content || '',
    citations: source.citations || [],
    modelUsed: source.modelUsed,
    usage: source.usage,
  }
}

/**
 * Drives one assistant response via the real SSE stream, updating the
 * placeholder message in place as chunks arrive. This is what gives the
 * "live streamed tokens" experience — content grows incrementally as
 * `status: 'streaming'`, and the typewriter effect in Message.jsx is never
 * enabled for these messages (no `isNew` flag is set on the streamed
 * completion), since the text was already shown live, token by token.
 *
 * If the stream transport itself fails (not aborted by the user, a genuine
 * connection error), falls back to one plain non-streaming request. That
 * fallback response *does* get `isNew: true`, since it arrives as a single
 * complete blob — this is the one path where the typewriter effect actually
 * fires, exactly as intended: a fallback for non-streaming responses, never
 * a replacement for real streaming.
 */
async function streamAssistantResponse(set, get, chatId, requestId, prompt) {
  const controller = new AbortController()
  set((state) => ({
    activeRequest: state.activeRequest && { ...state.activeRequest, abortController: controller },
  }))

  const isStale = () => {
    const request = get().activeRequest
    return !request || request.id !== requestId
  }

  // Soft-pin provider for this request — the backend falls back to the rest of
  // each task's chain when the pinned provider is exhausted or down.
  const provider = get().settings?.provider

  try {
    await sendMessageStream(
      chatId,
      prompt,
      (chunkData) => {
        if (isStale()) return
        const request = get().activeRequest

        set((state) => {
          const chatMessages = state.messagesByChatId[chatId] || []
          return {
            messagesByChatId: {
              ...state.messagesByChatId,
              [chatId]: chatMessages.map((message) => {
                if (message.id !== request.placeholderId) return message
                if (chunkData.type === 'thinking') {
                  // Live reasoning step — append to the thinking trace.
                  return { ...message, thinking: [...(message.thinking || []), chunkData.step] }
                }
                if (chunkData.type === 'chunk') {
                  return { ...message, content: message.content + chunkData.text, status: 'streaming' }
                }
                if (chunkData.type === 'done') {
                  const snapshot = versionSnapshot(chunkData.message)
                  if (request.mode === 'regenerate') {
                    // Append the new answer as a version on the SAME message so
                    // the user can page back to earlier ones.
                    const versions = [...(message.versions || []), snapshot]
                    return {
                      ...message,
                      content: snapshot.content,
                      citations: snapshot.citations,
                      modelUsed: snapshot.modelUsed,
                      usage: chunkData.message.usage,
                      thinking: chunkData.message.thinking || [],
                      versions,
                      activeVersion: versions.length - 1,
                      status: 'done',
                    }
                  }
                  return { ...chunkData.message, status: 'done', versions: [snapshot], activeVersion: 0 }
                }
                if (chunkData.type === 'error') {
                  if (request.mode === 'regenerate') {
                    // Drop the failed attempt; restore the last good version.
                    const versions = message.versions || []
                    const active = message.activeVersion ?? Math.max(0, versions.length - 1)
                    const restore = versions[active] || { content: message.content, citations: message.citations }
                    return { ...message, content: restore.content, citations: restore.citations || [], status: 'done' }
                  }
                  return { ...chunkData.message, status: 'error' }
                }
                return message
              }),
            },
          }
        })
      },
      controller.signal,
      provider,
    )

    if (isStale()) return
    set((state) => ({
      chats: touchChat(state.chats, chatId),
      activeRequest: null,
      loading: false,
    }))
  } catch (error) {
    if (error?.name === 'AbortError') {
      // User pressed stop. Leave whatever content already streamed in place
      // rather than discarding it — it's a real, if incomplete, answer.
      set((state) => {
        const request = state.activeRequest
        const chatMessages = state.messagesByChatId[chatId] || []
        return {
          messagesByChatId: {
            ...state.messagesByChatId,
            [chatId]: chatMessages.map((message) => {
              if (!request || message.id !== request.placeholderId) return message
              if (request.mode === 'regenerate') {
                // Keep the aborted partial as a version so the switcher and the
                // displayed content stay in sync.
                const versions = [...(message.versions || []), versionSnapshot(message)]
                return { ...message, versions, activeVersion: versions.length - 1, status: 'done' }
              }
              return { ...message, status: 'done' }
            }),
          },
          activeRequest: null,
          loading: false,
        }
      })
      return
    }

    if (isStale()) return
    console.warn('Streaming request failed, falling back to a non-streaming request:', error)

    try {
      const assistantMessage = await sendMessage(chatId, prompt, provider)
      if (isStale()) return
      set((state) => {
        const request = state.activeRequest
        const chatMessages = state.messagesByChatId[chatId] || []
        const snapshot = versionSnapshot(assistantMessage)
        return {
          messagesByChatId: {
            ...state.messagesByChatId,
            [chatId]: chatMessages.map((message) => {
              if (!request || message.id !== request.placeholderId) return message
              if (request.mode === 'regenerate') {
                const versions = [...(message.versions || []), snapshot]
                return {
                  ...message,
                  content: snapshot.content,
                  citations: snapshot.citations,
                  modelUsed: snapshot.modelUsed,
                  usage: snapshot.usage,
                  versions,
                  activeVersion: versions.length - 1,
                  status: 'done',
                  isNew: true,
                }
              }
              return { ...assistantMessage, status: 'done', isNew: true, versions: [snapshot], activeVersion: 0 }
            }),
          },
          chats: touchChat(state.chats, chatId),
          activeRequest: null,
          loading: false,
        }
      })
    } catch (fallbackError) {
      console.error('Non-streaming fallback also failed:', fallbackError)
      set((state) => {
        const request = state.activeRequest
        const chatMessages = state.messagesByChatId[chatId] || []
        return {
          messagesByChatId: {
            ...state.messagesByChatId,
            [chatId]: chatMessages.map((message) =>
              request && message.id === request.placeholderId
                ? {
                    ...message,
                    content: "Sorry, I wasn't able to reach the backend. Please try again.",
                    status: 'error',
                  }
                : message,
            ),
          },
          activeRequest: null,
          loading: false,
        }
      })
    }
  }
}

export const useAppStore = create((set, get) => ({
  chats: demoChats,
  activeChatId: demoChats[0].id,
  messagesByChatId: {},
  documents: [],
  overview: null,
  settings: null,
  providers: [],
  providerUsage: [],
  loading: false,
  sidebarOpen: false,
  sidebarCollapsed: readStoredBoolean('dataweave-sidebar-collapsed', false),
  selectedDocId: null,
  activeRequest: null,

  initApp: async () => {
    set({ loading: true })
    try {
      const [overview, chats, settings, documents, providerInfo] = await Promise.all([
        getOverview(),
        getChats(),
        getSettings(),
        getDocuments(),
        getProviders().catch(() => ({ providers: [], default: 'auto' })),
      ])

      const providerOptions = normalizeList(providerInfo?.providers, [])
      const defaultProvider = providerInfo?.default || 'auto'

      const mergedSettings = {
        endpoint: '/api',
        model: 'Mistral 7B Instruct',
        streamResponses: true,
        autoSync: true,
        theme: 'academic-dark',
        provider: defaultProvider,
        ...settings,
      }

      try {
        const savedSettings = localStorage.getItem('dataweave-settings')
        if (savedSettings) {
          Object.assign(mergedSettings, JSON.parse(savedSettings))
        }
      } catch {
        // Ignore storage issues and fall back to demo defaults.
      }

      // Guard against a stale saved provider that's no longer offered (e.g. its
      // API key was removed) — fall back to the server's default.
      const offered = new Set(providerOptions.map((p) => p.id))
      if (offered.size && !offered.has(mergedSettings.provider)) {
        mergedSettings.provider = defaultProvider
      }

      const normalizedChats = normalizeList(chats, demoChats)
      const normalizedDocuments = normalizeList(documents, [])
      const activeChatId = normalizedChats?.[0]?.id || get().activeChatId
      const messages = activeChatId ? await getMessages(activeChatId) : []

      set({
        overview,
        chats: normalizedChats.length ? normalizedChats : demoChats,
        activeChatId,
        messagesByChatId: { [activeChatId]: (messages || []).filter(Boolean) },
        settings: mergedSettings,
        providers: providerOptions,
        documents: normalizedDocuments,
      })
    } finally {
      set({ loading: false })
    }

    // Fire-and-forget: on first app load, scan the server's inbox folder and
    // show a side popup with the outcome. Never block the UI on it.
    get().runInboxScan()

    // Load the provider quota meters in the background — never block startup.
    get().refreshProviderUsage()
  },

  // Pull the live per-provider quota usage (RPM/RPD) from the shared limiter.
  // Best-effort: a failure just leaves the meters empty rather than erroring.
  refreshProviderUsage: async () => {
    try {
      const data = await getProviderUsage()
      set({ providerUsage: normalizeList(data?.providers, []) })
    } catch {
      // Leave whatever we had; the usage meter is non-critical.
    }
  },

  // Scan the watched drop-folder once and surface the result as a persistent
  // side toast (dismissible via its close cross). New files are ingested;
  // already-ingested files are reported as duplicates and skipped.
  runInboxScan: async () => {
    const toastId = toast.loading('Checking inbox folder…')
    try {
      const result = await scanIngestFolder()
      const { scanned = 0, ingested = 0, skipped = 0, failed = 0 } = result || {}

      // Empty folder → nothing to do.
      if (scanned === 0) {
        toast.info('No files found', {
          id: toastId,
          description: 'Nothing in the inbox folder to ingest.',
          duration: Infinity,
        })
        return
      }

      // Secondary details: duplicates skipped, and any failures.
      const details = []
      if (skipped > 0) {
        details.push(`${skipped} duplicate${plural(skipped)} found — already ingested, skipped`)
      }
      if (failed > 0) {
        details.push(`${failed} file${plural(failed)} failed to ingest`)
      }
      const description = details.join(' · ') || undefined

      if (ingested > 0) {
        // New content was added — refresh the document list to reflect it.
        get().refreshDocuments()
        const notify = failed > 0 ? toast.warning : toast.success
        notify(`${ingested} new file${plural(ingested)} ingested`, {
          id: toastId,
          description,
          duration: Infinity,
        })
      } else {
        toast.info('No new files', {
          id: toastId,
          description: description || 'Everything in the inbox folder is already ingested.',
          duration: Infinity,
        })
      }
    } catch {
      toast.error('Inbox scan failed', {
        id: toastId,
        description: 'Could not scan the inbox folder. Please try again later.',
        duration: Infinity,
      })
    }
  },

  selectChat: async (chatId) => {
    set({ activeChatId: chatId, sidebarOpen: false })
    const { messagesByChatId } = get()
    if (messagesByChatId[chatId]) return
    const messages = await getMessages(chatId)
    set((state) => ({
      messagesByChatId: { ...state.messagesByChatId, [chatId]: (messages || []).filter(Boolean) },
    }))
  },

  toggleSidebar: () => set((state) => ({ sidebarOpen: !state.sidebarOpen })),
  closeSidebar: () => set({ sidebarOpen: false }),
  toggleSidebarCollapse: () =>
    set((state) => {
      const nextValue = !state.sidebarCollapsed
      writeStoredBoolean('dataweave-sidebar-collapsed', nextValue)
      return { sidebarCollapsed: nextValue }
    }),

  newChat: async () => {
    const chat = await createChat('New Chat')
    set((state) => ({
      chats: [{ ...chat, title: 'New Chat', isUntitled: true }, ...normalizeList(state.chats, demoChats)],
      activeChatId: chat.id,
      sidebarOpen: false,
      messagesByChatId: { ...state.messagesByChatId, [chat.id]: [] },
    }))
  },

  renameChat: async (chatId, title) => {
    const nextTitle = title.trim()
    if (!chatId || !nextTitle) return

    set((state) => ({
      chats: state.chats.map((chat) =>
        chat.id === chatId
          ? { ...chat, title: nextTitle, isUntitled: false, updatedAt: new Date().toISOString() }
          : chat,
      ),
    }))

    try {
      await renameChatApi(chatId, nextTitle)
    } catch (error) {
      console.error('renameChat failed to persist:', error)
    }
  },

  finalizeChatTitle: async (chatId) => {
    const chat = get().chats.find((entry) => entry.id === chatId)
    // Only auto-title once, and never override a name the user set manually.
    if (!chat || !chat.isUntitled) return

    // Clear the flag up front so concurrent completions don't double-fire.
    set((state) => ({
      chats: state.chats.map((entry) =>
        entry.id === chatId ? { ...entry, isUntitled: false } : entry,
      ),
    }))

    try {
      const { title } = await generateChatTitle(chatId)
      const clean = (title || '').trim()
      if (clean) {
        set((state) => ({
          chats: state.chats.map((entry) =>
            entry.id === chatId ? { ...entry, title: clean } : entry,
          ),
        }))
      }
    } catch (error) {
      // Keep the optimistic placeholder title on failure.
      console.warn('Chat title generation failed:', error)
    }
  },

  deleteChat: async (chatId) => {
    if (!chatId) return

    const state = get()
    const remainingChats = state.chats.filter((chat) => chat.id !== chatId)
    const restMessages = { ...state.messagesByChatId }
    delete restMessages[chatId]

    try {
      await deleteChatApi(chatId)
    } catch (error) {
      console.error('deleteChat failed to persist:', error)
    }

    if (!remainingChats.length) {
      const chat = await createChat('New Chat')
      set({
        chats: [{ ...chat, title: 'New Chat', isUntitled: true }],
        activeChatId: chat.id,
        messagesByChatId: { [chat.id]: [] },
        sidebarOpen: false,
      })
      return
    }

    set({
      chats: remainingChats,
      activeChatId: state.activeChatId === chatId ? remainingChats[0].id : state.activeChatId,
      messagesByChatId: restMessages,
      sidebarOpen: false,
    })
  },

  deleteAllChats: async () => {
    const state = get()
    const chatIds = state.chats.map((chat) => chat.id)
    if (!chatIds.length) return

    // Best-effort: clear every chat server-side (there's no bulk-delete
    // endpoint), but don't let one failed delete block the rest or stop the
    // UI from resetting — same fire-and-log posture as the single-chat delete.
    await Promise.allSettled(chatIds.map((chatId) => deleteChatApi(chatId)))

    const chat = await createChat('New Chat')
    set({
      chats: [{ ...chat, title: 'New Chat', isUntitled: true }],
      activeChatId: chat.id,
      messagesByChatId: { [chat.id]: [] },
      sidebarOpen: false,
    })
  },

  sendPrompt: async (content) => {
    const { activeChatId } = get()
    if (!activeChatId || !content.trim() || get().activeRequest) return

    const prompt = content.trim()
    const activeChat = get().chats.find((chat) => chat.id === activeChatId)

    const requestId = ++requestSequence
    const placeholder = createLoadingAssistantMessage(requestId)

    const userMessage = {
      id: `user-${Date.now()}`,
      role: 'user',
      content: prompt,
      createdAt: new Date().toISOString(),
    }

    set((state) => ({
      messagesByChatId: {
        ...state.messagesByChatId,
        [activeChatId]: [...(state.messagesByChatId[activeChatId] || []), userMessage, placeholder],
      },
      activeRequest: { id: requestId, chatId: activeChatId, placeholderId: placeholder.id },
      loading: true,
    }))

    // Optimistically show a trimmed placeholder so the sidebar isn't stuck on
    // "New Chat" during the response. isUntitled stays true so the real
    // topic-aware title (generated from the first exchange) replaces it once
    // the answer lands — see finalizeChatTitle.
    if (activeChat?.isUntitled) {
      set((state) => ({
        chats: state.chats.map((chat) =>
          chat.id === activeChatId ? { ...chat, title: buildUntitledChatTitle(prompt) } : chat,
        ),
      }))
    }

    await streamAssistantResponse(set, get, activeChatId, requestId, prompt)
    get().finalizeChatTitle(activeChatId)
  },

  stopGeneration: () => {
    const request = get().activeRequest
    if (!request?.abortController) return
    request.abortController.abort()
    // streamAssistantResponse's catch(AbortError) branch handles the resulting state update.
  },

  setMessageFeedback: (chatId, messageId, value) => {
    if (!chatId || !messageId) return

    let nextFeedback = null
    set((state) => ({
      messagesByChatId: {
        ...state.messagesByChatId,
        [chatId]: (state.messagesByChatId[chatId] || []).map((message) => {
          if (message.id !== messageId) return message
          nextFeedback = message.feedback === value ? null : value
          return { ...message, feedback: nextFeedback }
        }),
      },
    }))
    // Persist server-side (fire-and-forget) so the rating survives a reload.
    // The UI already updated optimistically above.
    setMessageFeedbackApi(chatId, messageId, nextFeedback).catch((error) => {
      console.warn('Failed to persist message feedback:', error)
    })
  },

  submitFeedbackComment: (chatId, messageId, comment) => {
    if (!chatId || !messageId) return

    set((state) => ({
      messagesByChatId: {
        ...state.messagesByChatId,
        [chatId]: (state.messagesByChatId[chatId] || []).map((message) => {
          if (message.id !== messageId) return message
          return { ...message, feedbackComment: comment }
        }),
      },
    }))

    submitFeedbackComment(chatId, messageId, comment).catch((error) => {
      console.warn('Failed to persist message feedback comment:', error)
    })
  },

  regenerateMessage: async (chatId, messageId) => {
    if (!chatId || !messageId || get().activeRequest) return

    const state = get()
    const messages = state.messagesByChatId[chatId] || []
    const messageIndex = messages.findIndex((message) => message.id === messageId)
    const message = messages[messageIndex]
    if (!message || message.role !== 'assistant' || message.status === 'loading' || message.status === 'streaming') return
    if (messageIndex !== messages.length - 1) return

    const previousUserMessage = [...messages.slice(0, messageIndex)]
      .reverse()
      .find((entry) => entry.role === 'user')
    if (!previousUserMessage) return

    const requestId = ++requestSequence

    // Stream the new attempt into the SAME message instead of replacing it:
    // seed the version archive with the current answer (if not already
    // versioned), then clear the display so the regenerated tokens stream in.
    // The done handler appends the result as a new version — see
    // streamAssistantResponse — so the user can page between them.
    set((currentState) => ({
      messagesByChatId: {
        ...currentState.messagesByChatId,
        [chatId]: (currentState.messagesByChatId[chatId] || []).map((m) => {
          if (m.id !== messageId) return m
          const versions = m.versions?.length ? m.versions : [versionSnapshot(m)]
          // Reset the live thinking trace so the regenerated attempt starts clean.
          return { ...m, versions, content: '', thinking: [], status: 'loading' }
        }),
      },
      activeRequest: { id: requestId, chatId, placeholderId: messageId, mode: 'regenerate' },
      chats: touchChat(currentState.chats, chatId),
      loading: true,
    }))

    // Note: the /messages/stream endpoint still persists each regenerated
    // answer as a new turn server-side, so the version history is in-session
    // only — a reload collapses it back to separate messages.
    await streamAssistantResponse(set, get, chatId, requestId, previousUserMessage.content)
  },

  setMessageVersion: (chatId, messageId, index) => {
    if (!chatId || !messageId) return
    set((state) => ({
      messagesByChatId: {
        ...state.messagesByChatId,
        [chatId]: (state.messagesByChatId[chatId] || []).map((message) => {
          if (message.id !== messageId || !message.versions?.length) return message
          const clamped = Math.max(0, Math.min(index, message.versions.length - 1))
          const version = message.versions[clamped]
          return {
            ...message,
            activeVersion: clamped,
            content: version.content,
            citations: version.citations || [],
            modelUsed: version.modelUsed,
            usage: version.usage,
          }
        }),
      },
    }))
  },

  editMessage: async (chatId, messageId, newContent) => {
    if (!chatId || !messageId || get().activeRequest) return

    const nextContent = newContent.trim()
    if (!nextContent) return

    const state = get()
    const messages = state.messagesByChatId[chatId] || []
    const messageIndex = messages.findIndex((message) => message.id === messageId)
    const message = messages[messageIndex]
    if (!message || message.role !== 'user') return

    const requestId = ++requestSequence
    const placeholder = createLoadingAssistantMessage(requestId)
    const updatedUserMessage = { ...message, content: nextContent, editedAt: new Date().toISOString() }

    set((currentState) => ({
      messagesByChatId: {
        ...currentState.messagesByChatId,
        [chatId]: [...messages.slice(0, messageIndex), updatedUserMessage, placeholder],
      },
      activeRequest: { id: requestId, chatId, placeholderId: placeholder.id },
      chats: touchChat(currentState.chats, chatId),
      loading: true,
    }))

    // Same caveat as regenerateMessage: the backend will persist this as a
    // new user turn rather than truly replacing the original message.
    await streamAssistantResponse(set, get, chatId, requestId, nextContent)
  },

  markMessageAsSeen: (chatId, messageId) => {
    if (!chatId || !messageId) return

    set((state) => ({
      messagesByChatId: {
        ...state.messagesByChatId,
        [chatId]: (state.messagesByChatId[chatId] || []).map((message) =>
          message.id === messageId ? { ...message, isNew: false } : message,
        ),
      },
    }))
  },

  uploadDocument: async (file) => {
    const uploaded = await uploadDocument(file)
    await get().refreshDocuments()
    return uploaded
  },

  // Upload + ingest a file into its own chat, streaming the 10-stage pipeline
  // progress into a persistent card (kind: 'ingestion') that survives reload.
  ingestDocument: async (file) =>
    get()._streamIngestionCard({
      file,
      title: `📄 ${file.name}`,
      stream: (onEvent) => uploadDocumentStream(file, onEvent),
      doneVerb: 'Ingested',
      failVerb: 'Ingestion',
    }),

  // Replace an existing document with a new file. Uses the same streaming card
  // so the user sees the full pipeline run; the backend keeps the old version
  // live until the new one is fully indexed (safe atomic cutover).
  replaceDocument: async (oldDocumentId, file) =>
    get()._streamIngestionCard({
      file,
      title: `♻️ Replace → ${file.name}`,
      stream: (onEvent) => replaceDocumentStream(oldDocumentId, file, onEvent),
      doneVerb: 'Replaced with',
      failVerb: 'Replace',
    }),

  deleteDocument: async (documentId) => {
    await deleteDocumentApi(documentId)
    await get().refreshDocuments()
  },

  // Shared driver for ingest/replace: opens a chat, streams stage events into a
  // persistent ingestion card, refreshes the document list, and persists the
  // finished card so its step trace survives a reload.
  _streamIngestionCard: async ({ file, title, stream, doneVerb, failVerb }) => {
    const chat = await createChat(title)
    const messageId = `ingest-${Date.now()}`
    const card = {
      id: messageId,
      role: 'assistant',
      kind: 'ingestion',
      fileName: file.name,
      status: 'running',
      steps: INGESTION_STAGES.map((label, i) => ({
        stage: i + 1,
        label,
        status: 'pending',
        detail: '',
      })),
      summary: null,
      content: '',
      createdAt: new Date().toISOString(),
      chatId: chat.id,
    }

    set((state) => ({
      chats: [
        { ...chat, title, isUntitled: false },
        ...normalizeList(state.chats, demoChats),
      ],
      activeChatId: chat.id,
      messagesByChatId: { ...state.messagesByChatId, [chat.id]: [card] },
      sidebarOpen: false,
    }))

    const patchCard = (updater) =>
      set((state) => ({
        messagesByChatId: {
          ...state.messagesByChatId,
          [chat.id]: (state.messagesByChatId[chat.id] || []).map((m) =>
            m.id === messageId ? { ...m, ...updater(m) } : m,
          ),
        },
      }))

    try {
      await stream((event) => {
        if (event.type === 'progress') {
          patchCard((m) => ({
            steps: m.steps.map((s) =>
              s.stage === event.stage
                ? { ...s, status: event.status, detail: event.detail || s.detail }
                : s,
            ),
          }))
        } else if (event.type === 'skipped') {
          patchCard(() => ({ status: 'skipped' }))
        } else if (event.type === 'complete') {
          const result = event.result || {}
          patchCard((m) => ({
            status: event.skipped ? 'skipped' : 'done',
            // Any stage not explicitly closed out (e.g. skipped uploads) is
            // resolved so no spinner is left hanging.
            steps: m.steps.map((s) =>
              s.status === 'pending' || s.status === 'running'
                ? { ...s, status: event.skipped ? 'skipped' : 'done' }
                : s,
            ),
            summary: {
              totalChunks: result.total_chunks ?? 0,
              totalPages: result.total_pages ?? 0,
              documentType: result.document_type,
            },
            content: event.skipped
              ? `${file.name} was already ingested.`
              : `${doneVerb} ${file.name}: ${result.total_chunks ?? 0} chunks across ${result.total_pages ?? 0} page(s).`,
          }))
        } else if (event.type === 'error') {
          patchCard(() => ({ status: 'error', content: `${failVerb} failed: ${event.message || 'unknown error'}` }))
        }
      })
    } catch (error) {
      console.error(`${failVerb} stream failed:`, error)
      patchCard(() => ({ status: 'error', content: `${failVerb} failed — check server logs.` }))
    }

    await get().refreshDocuments()

    // Persist the finished card so the step trace stays after a reload.
    const finalCard = (get().messagesByChatId[chat.id] || []).find((m) => m.id === messageId)
    if (finalCard) {
      try {
        await persistIngestionCard(chat.id, {
          id: finalCard.id,
          fileName: finalCard.fileName,
          status: finalCard.status,
          steps: finalCard.steps,
          summary: finalCard.summary,
          content: finalCard.content,
          createdAt: finalCard.createdAt,
        })
      } catch (error) {
        console.warn('Failed to persist ingestion card:', error)
      }
    }

    return finalCard
  },

  refreshDocuments: async () => {
    const documents = await getDocuments()
    set({ documents: normalizeList(documents, []) })
  },

  updateSettings: async (patch) => {
    const settings = { ...(get().settings || {}), ...patch }
    set({ settings })
    try {
      localStorage.setItem('dataweave-settings', JSON.stringify(settings))
    } catch {
      // Ignore storage write errors; the local demo state still updates.
    }
    await saveSettings(settings)
  },

  selectDocument: (docId) => set({ selectedDocId: docId }),
}))
