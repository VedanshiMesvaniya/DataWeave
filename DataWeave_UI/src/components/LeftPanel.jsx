import { useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import dayjs from 'dayjs'
import { FileText, MoreVertical, PlusCircle, RotateCcw, Trash2, Upload } from 'lucide-react'
import { useNavigate } from 'react-router-dom'
import { toast } from 'sonner'
import { useAppStore } from '../store/store.js'

function fileExtension(name) {
  const dot = (name || '').lastIndexOf('.')
  return dot > 0 ? name.slice(dot + 1).toUpperCase() : 'FILE'
}

export default function LeftPanel() {
  const documents = useAppStore((state) => state.documents)
  const replaceDocument = useAppStore((state) => state.replaceDocument)
  const deleteDocument = useAppStore((state) => state.deleteDocument)
  const ingestDocument = useAppStore((state) => state.ingestDocument)
  const newChat = useAppStore((state) => state.newChat)
  const creatingChat = useAppStore((state) => state.creatingChat)
  const navigate = useNavigate()

  const [openMenuId, setOpenMenuId] = useState(null)
  const [menuPosition, setMenuPosition] = useState(null)
  const [busyId, setBusyId] = useState(null)
  const [isUploading, setIsUploading] = useState(false)
  const [dialog, setDialog] = useState({ type: null, doc: null })

  const replaceInputRef = useRef(null)
  const replaceTargetRef = useRef(null)
  const uploadInputRef = useRef(null)

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

  const toggleDocMenu = (doc, event) => {
    const triggerRect = event.currentTarget.getBoundingClientRect()
    const menuWidth = 176
    const menuHeight = 96
    const viewportWidth = window.innerWidth
    const viewportHeight = window.innerHeight
    const nextLeft = Math.max(12, Math.min(triggerRect.right - menuWidth, viewportWidth - menuWidth - 12))
    const enoughRoomBelow = triggerRect.bottom + menuHeight + 12 <= viewportHeight

    if (openMenuId === doc.id) {
      closeMenu()
      return
    }

    setOpenMenuId(doc.id)
    setMenuPosition(
      enoughRoomBelow
        ? { top: triggerRect.bottom + 8, left: nextLeft }
        : { bottom: viewportHeight - triggerRect.top + 8, left: nextLeft },
    )
  }

  const openReplacePicker = (docId) => {
    closeMenu()
    replaceTargetRef.current = docId
    if (replaceInputRef.current) {
      replaceInputRef.current.value = ''
      replaceInputRef.current.click()
    }
  }

  const onReplaceFileChosen = async (event) => {
    const file = event.target.files?.[0]
    const targetId = replaceTargetRef.current
    if (!file || !targetId) return
    setBusyId(targetId)
    try {
      navigate('/chat')
      await replaceDocument(targetId, file)
      toast.success('Document re-uploaded.')
    } catch (error) {
      console.error(error)
      toast.error('Re-upload failed. Check server logs.')
    } finally {
      setBusyId(null)
      replaceTargetRef.current = null
    }
  }

  const askDelete = (doc) => {
    closeMenu()
    setDialog({ type: 'delete', doc })
  }

  const confirmDelete = async () => {
    const doc = dialog.doc
    if (!doc) return
    setBusyId(doc.id)
    try {
      await deleteDocument(doc.id)
      toast.success('Document deleted.')
    } catch (error) {
      console.error(error)
      toast.error('Delete failed. Check server logs.')
    } finally {
      setBusyId(null)
      setDialog({ type: null, doc: null })
    }
  }

  const handleUploadClick = () => {
    uploadInputRef.current?.click()
  }

  const handleUploadFileChosen = async (event) => {
    const file = event.target.files?.[0]
    if (!file) return
    try {
      setIsUploading(true)
      navigate('/chat')
      await ingestDocument(file)
    } catch (error) {
      console.error(error)
      toast.error('Upload failed. Check server logs.')
    } finally {
      setIsUploading(false)
      if (uploadInputRef.current) uploadInputRef.current.value = ''
    }
  }

  const handleNewChat = async () => {
    await newChat()
    navigate('/chat')
  }

  return (
    <div className="panel-col panel-col--left">
      <section className="glass-card doc-card">
        <p className="glass-card__title">Document list</p>
        <div className="doc-card__list scrollbar-auto">
          {documents.length === 0 ? (
            <p className="doc-card__empty">No documents yet. Upload one from Tools below.</p>
          ) : (
            documents.map((doc) => (
              <div key={doc.id} className={`doc-item ${busyId === doc.id ? 'doc-item--busy' : ''}`}>
                <span className="doc-item__icon">
                  <FileText size={16} />
                </span>
                <div className="doc-item__body">
                  <p className="doc-item__name">{doc.name}</p>
                  <p className="doc-item__meta">
                    {fileExtension(doc.name)}
                    {doc.chunks != null ? ` · ${doc.chunks} chunks` : ''}
                    {doc.ingestedAt ? ` · Added ${dayjs(doc.ingestedAt).format('MMM D, HH:mm')}` : ''}
                  </p>
                </div>
                <button
                  type="button"
                  className="doc-item__menu-trigger"
                  aria-label={`Actions for ${doc.name}`}
                  onClick={(event) => toggleDocMenu(doc, event)}
                  disabled={busyId === doc.id}
                >
                  <MoreVertical size={14} />
                </button>

                {openMenuId === doc.id
                  ? createPortal(
                      <div className="chat-menu-backdrop" role="presentation" onClick={closeMenu}>
                        <div
                          className="chat-menu"
                          role="menu"
                          aria-label="Document actions"
                          style={menuPosition ?? undefined}
                          onClick={(event) => event.stopPropagation()}
                        >
                          <button
                            type="button"
                            className="chat-menu__item"
                            role="menuitem"
                            onClick={() => openReplacePicker(doc.id)}
                          >
                            <RotateCcw size={14} />
                            <span>Re-upload</span>
                          </button>
                          <button
                            type="button"
                            className="chat-menu__item chat-menu__item--danger"
                            role="menuitem"
                            onClick={() => askDelete(doc)}
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
            ))
          )}
        </div>
      </section>

      <section className="glass-card tools-card">
        <p className="glass-card__title">Tools</p>
        <div className="tools-card__actions">
          <button type="button" className="tool-button" disabled={creatingChat} onClick={handleNewChat}>
            <PlusCircle size={16} />
            <span>{creatingChat ? 'Creating…' : 'New Chat'}</span>
          </button>
          <button type="button" className="tool-button" disabled={isUploading} onClick={handleUploadClick}>
            <Upload size={16} />
            <span>{isUploading ? 'Uploading…' : 'Document Upload'}</span>
          </button>
        </div>
      </section>

      <input ref={replaceInputRef} type="file" style={{ display: 'none' }} onChange={onReplaceFileChosen} />
      <input ref={uploadInputRef} type="file" style={{ display: 'none' }} onChange={handleUploadFileChosen} />

      {dialog.type === 'delete'
        ? createPortal(
            <div className="dialog-backdrop" role="presentation" onClick={() => setDialog({ type: null, doc: null })}>
              <div
                className="dialog-card"
                role="dialog"
                aria-modal="true"
                aria-labelledby="doc-dialog-title"
                onClick={(event) => event.stopPropagation()}
              >
                <p className="dialog-card__eyebrow">Document action</p>
                <h3 id="doc-dialog-title" className="dialog-card__title">
                  Delete document
                </h3>
                <p className="dialog-card__text">
                  This will remove &quot;{dialog.doc?.name}&quot; from the knowledge base.
                </p>
                <div className="dialog-card__actions">
                  <button type="button" className="secondary-button" onClick={() => setDialog({ type: null, doc: null })}>
                    Cancel
                  </button>
                  <button type="button" className="primary-button primary-button--danger" onClick={confirmDelete}>
                    Delete
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
