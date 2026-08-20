import {
  FileText,
  MessageSquareText,
  MoreVertical,
  PlusCircle,
  PanelLeftClose,
  Settings,
  Trash2,
  PencilLine,
} from 'lucide-react'
import dayjs from 'dayjs'
import { useEffect, useState } from 'react'
import { createPortal } from 'react-dom'
import { NavLink, useNavigate } from 'react-router-dom'
import { useAppStore } from '../store/store.js'

const primaryNavItems = [
  { to: '/chat', label: 'Chat', icon: MessageSquareText },
  { to: '/documents', label: 'Documents', icon: FileText },
]

const secondaryNavItems = [{ to: '/settings', label: 'Settings', icon: Settings }]

export default function Sidebar() {
  const chats = useAppStore((state) => state.chats)
  const activeChatId = useAppStore((state) => state.activeChatId)
  const selectChat = useAppStore((state) => state.selectChat)
  const newChat = useAppStore((state) => state.newChat)
  const renameChat = useAppStore((state) => state.renameChat)
  const deleteChat = useAppStore((state) => state.deleteChat)
  const sidebarOpen = useAppStore((state) => state.sidebarOpen)
  const sidebarCollapsed = useAppStore((state) => state.sidebarCollapsed)
  const toggleSidebarCollapse = useAppStore((state) => state.toggleSidebarCollapse)
  const closeSidebar = useAppStore((state) => state.closeSidebar)
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

  const handleNewChat = async () => {
    setOpenMenuId(null)
    setMenuPosition(null)
    await newChat()
    navigate('/chat')
  }

  const handleRename = async (chat) => {
    setOpenMenuId(null)
    setMenuPosition(null)
    setDialog({ type: 'rename', chat, value: chat.title })
  }

  const handleDelete = async (chat) => {
    setOpenMenuId(null)
    setMenuPosition(null)
    setDialog({ type: 'delete', chat, value: '' })
  }

  const closeDialog = () => setDialog({ type: null, chat: null, value: '' })

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

  const confirmDialog = async () => {
    if (!dialog.chat) return

    if (dialog.type === 'rename') {
      const nextTitle = dialog.value.trim()
      if (!nextTitle || nextTitle === dialog.chat.title) {
        closeDialog()
        return
      }
      await renameChat(dialog.chat.id, nextTitle)
    }

    if (dialog.type === 'delete') {
      await deleteChat(dialog.chat.id)
      navigate('/chat')
    }

    closeDialog()
  }

  return (
    <>
      <aside className="sidebar" data-open={sidebarOpen} data-collapsed={sidebarCollapsed}>
        <div className="brand">
          <div className="brand__row">
            <div>
              <h1 className="brand__title">DataWeave</h1>
              <p className="brand__subtitle">Data - Decisions</p>
            </div>
            <button
              type="button"
              className="icon-button desktop-toggle"
              onClick={toggleSidebarCollapse}
              aria-label="Collapse sidebar"
            >
              <PanelLeftClose size={18} />
            </button>
          </div>
        </div>

        <div className="sidebar__new-chat-row">
          <button
            type="button"
            onClick={handleNewChat}
            className="new-chat-action new-chat-action--full"
          >
            <PlusCircle size={18} />
            <span>New chat</span>
          </button>
        </div>

        <nav className="sidebar__nav" aria-label="Primary navigation">
          {primaryNavItems.map(({ to, label, icon: Icon }) => (
            <NavLink
              key={to}
              to={to}
              className={({ isActive }) =>
                `nav-item ${isActive ? 'nav-item--active' : ''}`
              }
              onClick={closeSidebar}
            >
              <Icon size={18} />
              <span>{label}</span>
            </NavLink>
          ))}
        </nav>

        <section className="sidebar__section sidebar__section--grow">
          <p className="section-title">Recent Chats</p>
          <div className="chat-list">
            {chats.map((chat) => (
              <div
                key={chat.id}
                className={`chat-item ${
                  activeChatId === chat.id ? 'chat-item--active' : ''
                } ${openMenuId === chat.id ? 'chat-item--menu-open' : ''}`}
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
                  <p className="chat-item__meta">
                    Updated {dayjs(chat.updatedAt).format('MMM D, HH:mm')}
                  </p>
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

                  {openMenuId === chat.id ? (
                    createPortal(
                      <div
                        className="chat-menu-backdrop"
                        role="presentation"
                        onClick={closeMenu}
                      >
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
                            onClick={() => handleRename(chat)}
                            role="menuitem"
                          >
                            <PencilLine size={14} />
                            <span>Rename</span>
                          </button>
                          <button
                            type="button"
                            className="chat-menu__item chat-menu__item--danger"
                            onClick={() => handleDelete(chat)}
                            role="menuitem"
                          >
                            <Trash2 size={14} />
                            <span>Delete</span>
                          </button>
                        </div>
                      </div>,
                      document.body,
                    )
                  ) : null}
                </div>
              </div>
            ))}
          </div>
        </section>

        <footer className="sidebar__footer">
          <nav className="sidebar__nav sidebar__nav--secondary" aria-label="Secondary navigation">
            {secondaryNavItems.map(({ to, label, icon: Icon }) => (
              <NavLink
                key={to}
                to={to}
                className={({ isActive }) =>
                  `nav-item ${isActive ? 'nav-item--active' : ''}`
                }
                onClick={closeSidebar}
              >
                <Icon size={18} />
                <span>{label}</span>
              </NavLink>
            ))}
          </nav>
        </footer>
      </aside>
      {sidebarOpen ? (
        <button
          type="button"
          className="sidebar-backdrop"
          onClick={closeSidebar}
          aria-label="Close navigation"
        />
      ) : null}

      {dialog.type ? (
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
              {dialog.type === 'rename' ? 'Rename chat' : 'Delete chat'}
            </h3>
            <p id="chat-dialog-description" className="dialog-card__text">
              {dialog.type === 'rename'
                ? 'Give this conversation a new name.'
                : `This will remove "${dialog.chat?.title}" from recent chats.`}
            </p>

            {dialog.type === 'rename' ? (
              <input
                autoFocus
                className="dialog-card__input"
                value={dialog.value}
                onChange={(event) =>
                  setDialog((current) => ({ ...current, value: event.target.value }))
                }
                placeholder="Chat title"
              />
            ) : null}

            <div className="dialog-card__actions">
              <button type="button" className="secondary-button" onClick={closeDialog}>
                Cancel
              </button>
              <button
                type="button"
                className={`primary-button ${
                  dialog.type === 'delete' ? 'primary-button--danger' : ''
                }`}
                onClick={confirmDialog}
              >
                {dialog.type === 'rename' ? 'Save changes' : 'Delete chat'}
              </button>
            </div>
          </div>
        </div>
      ) : null}
    </>
  )
}
