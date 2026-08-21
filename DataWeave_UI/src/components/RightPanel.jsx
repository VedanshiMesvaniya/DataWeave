import { useEffect, useState } from 'react'
import { createPortal } from 'react-dom'
import dayjs from 'dayjs'
import { MoreVertical, PencilLine, Trash2 } from 'lucide-react'
import { useNavigate } from 'react-router-dom'
import { useAppStore } from '../store/store.js'

export default function RightPanel() {
  const chats = useAppStore((state) => state.chats)
  const activeChatId = useAppStore((state) => state.activeChatId)
  const selectChat = useAppStore((state) => state.selectChat)
  const renameChat = useAppStore((state) => state.renameChat)
  const deleteChat = useAppStore((state) => state.deleteChat)
  const deleteAllChats = useAppStore((state) => state.deleteAllChats)
  const navigate = useNavigate()

  const [openMenuId, setOpenMenuId] = useState(null)
  const [menuPosition, setMenuPosition] = useState(null)
  const [dialog, setDialog] = useState({ type: null, chat: null, value: '' })

  useEffect(() => {
    if (!openMenuId) return undefined
    const handleViewportChange = () => {
      setOpenMenuId(null)
      setMenuPosition(null)
    }
    window.addEventListener('scroll', handleViewportChange, true)
    window.addEventListener('resize', handleViewportChange)
    return () => {
      window.removeEventListener('scroll', handleViewportChange, true)
      window.removeEventListener('resize', handleViewportChange)
    }
  }, [openMenuId])

  const closeMenu = () => {
    setOpenMenuId(null)
    setMenuPosition(null)
  }

  const toggleChatMenu = (chat, event) => {
    const triggerRect = event.currentTarget.getBoundingClientRect()
    const menuWidth = 168
    const menuHeight = 96
    const viewportWidth = window.innerWidth
    const viewportHeight = window.innerHeight
    const nextLeft = Math.max(12, Math.min(triggerRect.right - menuWidth, viewportWidth - menuWidth - 12))
    const enoughRoomBelow = triggerRect.bottom + menuHeight + 12 <= viewportHeight

    if (openMenuId === chat.id) {
      closeMenu()
      return
    }

    setOpenMenuId(chat.id)
    setMenuPosition(
      enoughRoomBelow
        ? { top: triggerRect.bottom + 8, left: nextLeft }
        : { bottom: viewportHeight - triggerRect.top + 8, left: nextLeft },
    )
  }

  const closeDialog = () => setDialog({ type: null, chat: null, value: '' })

  const confirmDialog = async () => {
    if (!dialog.chat) return
    if (dialog.type === 'rename') {
      const nextTitle = dialog.value.trim()
      if (nextTitle && nextTitle !== dialog.chat.title) {
        await renameChat(dialog.chat.id, nextTitle)
      }
    }
    if (dialog.type === 'delete') {
      await deleteChat(dialog.chat.id)
      navigate('/chat')
    }
    closeDialog()
  }

  const confirmDeleteAll = async () => {
    await deleteAllChats()
    setDialog({ type: null, chat: null, value: '' })
    navigate('/chat')
  }

  return (
    <div className="panel-col panel-col--right">
      <section className="glass-card recent-card">
        <div className="recent-card__header">
          <p className="glass-card__title recent-card__title">Recent chat</p>
          {chats.length ? (
            <button
              type="button"
              className="recent-card__clear-all"
              aria-label="Delete all chats"
              title="Delete all chats"
              onClick={() => setDialog({ type: 'delete-all', chat: null, value: '' })}
            >
              <Trash2 size={14} />
            </button>
          ) : null}
        </div>
        <div className="recent-card__list scrollbar-auto">
          {chats.map((chat) => (
            <div
              key={chat.id}
              className={`chat-item ${activeChatId === chat.id ? 'chat-item--active' : ''} ${
                openMenuId === chat.id ? 'chat-item--menu-open' : ''
              }`}
            >
              <button
                type="button"
                className="chat-item__main"
                onClick={async () => {
                  await selectChat(chat.id)
                  navigate('/chat')
                }}
              >
                <p className="chat-item__title">{chat.title}</p>
                <p className="chat-item__meta">Updated {dayjs(chat.updatedAt).format('MMM D, HH:mm')}</p>
              </button>

              <div className="chat-item__actions">
                <button
                  type="button"
                  className="chat-item__menu-trigger"
                  aria-label={`Chat actions for ${chat.title}`}
                  onClick={(event) => toggleChatMenu(chat, event)}
                >
                  <MoreVertical size={14} />
                </button>

                {openMenuId === chat.id
                  ? createPortal(
                      <div className="chat-menu-backdrop" role="presentation" onClick={closeMenu}>
                        <div
                          className="chat-menu"
                          role="menu"
                          aria-label="Chat actions"
                          style={menuPosition ?? undefined}
                          onClick={(event) => event.stopPropagation()}
                        >
                          <button
                            type="button"
                            className="chat-menu__item"
                            role="menuitem"
                            onClick={() => {
                              closeMenu()
                              setDialog({ type: 'rename', chat, value: chat.title })
                            }}
                          >
                            <PencilLine size={14} />
                            <span>Rename</span>
                          </button>
                          <button
                            type="button"
                            className="chat-menu__item chat-menu__item--danger"
                            role="menuitem"
                            onClick={() => {
                              closeMenu()
                              setDialog({ type: 'delete', chat, value: '' })
                            }}
                          >
                            <Trash2 size={14} />
                            <span>Delete</span>
                          </button>
                        </div>
                      </div>,
                      document.body,
                    )
                  : null}
              </div>
            </div>
          ))}
        </div>
      </section>

      {dialog.type
        ? createPortal(
            <div className="dialog-backdrop" role="presentation" onClick={closeDialog}>
              <div
                className="dialog-card"
                role="dialog"
                aria-modal="true"
                aria-labelledby="chat-dialog-title"
                aria-describedby="chat-dialog-description"
                onClick={(event) => event.stopPropagation()}
              >
                <p className="dialog-card__eyebrow">Chat action</p>
                <h3 id="chat-dialog-title" className="dialog-card__title">
                  {dialog.type === 'rename'
                    ? 'Rename chat'
                    : dialog.type === 'delete-all'
                      ? 'Delete all chats'
                      : 'Delete chat'}
                </h3>
                <p id="chat-dialog-description" className="dialog-card__text">
                  {dialog.type === 'rename'
                    ? 'Give this conversation a new name.'
                    : dialog.type === 'delete-all'
                      ? 'This will permanently remove every conversation in your recent chats. This cannot be undone.'
                      : `This will remove "${dialog.chat?.title}" from recent chats.`}
                </p>

                {dialog.type === 'rename' ? (
                  <input
                    autoFocus
                    className="dialog-card__input"
                    value={dialog.value}
                    onChange={(event) => setDialog((current) => ({ ...current, value: event.target.value }))}
                    placeholder="Chat title"
                  />
                ) : null}

                <div className="dialog-card__actions">
                  <button type="button" className="secondary-button" onClick={closeDialog}>
                    Cancel
                  </button>
                  <button
                    type="button"
                    className={`primary-button ${dialog.type !== 'rename' ? 'primary-button--danger' : ''}`}
                    onClick={dialog.type === 'delete-all' ? confirmDeleteAll : confirmDialog}
                  >
                    {dialog.type === 'rename'
                      ? 'Save changes'
                      : dialog.type === 'delete-all'
                        ? 'Delete all'
                        : 'Delete chat'}
                  </button>
                </div>
              </div>
            </div>,
            document.body,
          )
        : null}
    </div>
  )
}
