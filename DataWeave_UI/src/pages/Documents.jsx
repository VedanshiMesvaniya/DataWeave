import { useRef, useState } from 'react'
import dayjs from 'dayjs'
import { motion } from 'framer-motion'
import { Loader2, RefreshCw, RotateCcw, Trash2, Upload } from 'lucide-react'
import { useNavigate } from 'react-router-dom'
import { toast } from 'sonner'
import Button from '../components/Button.jsx'
import Loader from '../components/Loader.jsx'
import { useAppStore } from '../store/store.js'

function formatBytes(bytes) {
  const value = Number(bytes)
  if (!value || value < 0) return '—'
  const units = ['B', 'KB', 'MB', 'GB']
  let size = value
  let unit = 0
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024
    unit += 1
  }
  return `${size < 10 && unit > 0 ? size.toFixed(1) : Math.round(size)} ${units[unit]}`
}

function fileExtension(name) {
  const dot = (name || '').lastIndexOf('.')
  return dot > 0 ? name.slice(dot + 1).toUpperCase() : 'FILE'
}

export default function Documents() {
  const documents = useAppStore((state) => state.documents)
  const refreshDocuments = useAppStore((state) => state.refreshDocuments)
  const replaceDocument = useAppStore((state) => state.replaceDocument)
  const deleteDocument = useAppStore((state) => state.deleteDocument)
  const ingestDocument = useAppStore((state) => state.ingestDocument)
  const loading = useAppStore((state) => state.loading)

  const navigate = useNavigate()
  const fileInputRef = useRef(null)
  const replaceTargetRef = useRef(null)
  const uploadInputRef = useRef(null)
  const [busyId, setBusyId] = useState(null)
  const [isUploading, setIsUploading] = useState(false)

  const handleUploadClick = () => {
    uploadInputRef.current?.click()
  }

  const handleUploadFileChosen = async (event) => {
    const file = event.target.files?.[0]
    if (!file) return

    try {
      setIsUploading(true)
      // Opens a new chat and streams the live ingestion trace into it.
      navigate('/chat')
      await ingestDocument(file)
    } catch (error) {
      console.error(error)
      toast.error(`Upload failed. Check server logs.`)
    } finally {
      setIsUploading(false)
      if (uploadInputRef.current) {
        uploadInputRef.current.value = ''
      }
    }
  }

  const openReplacePicker = (docId) => {
    replaceTargetRef.current = docId
    if (fileInputRef.current) {
      fileInputRef.current.value = ''
      fileInputRef.current.click()
    }
  }

  const onReplaceFileChosen = async (event) => {
    const file = event.target.files?.[0]
    const targetId = replaceTargetRef.current
    if (!file || !targetId) return
    setBusyId(targetId)
    try {
      // Navigate first so the streaming ingestion card is visible as it runs.
      navigate('/chat')
      await replaceDocument(targetId, file)
    } finally {
      setBusyId(null)
      replaceTargetRef.current = null
    }
  }

  const onDelete = async (doc) => {
    if (!window.confirm(`Delete "${doc.name}"? This removes it from the knowledge base.`)) {
      return
    }
    setBusyId(doc.id)
    try {
      await deleteDocument(doc.id)
    } finally {
      setBusyId(null)
    }
  }

  return (
    <section className="page section">
      <div className="section__header">
        <div>
          <h2 className="section__title">Documents</h2>
          <p className="section__subtitle">
            The current version of every document in your knowledge base. Replace swaps in
            new content without losing the old version; delete removes it entirely.
          </p>
        </div>
        <div className="section__header-actions">
          <Button
            variant="secondary"
            className="section__header-btn"
            disabled={isUploading}
            onClick={handleUploadClick}
          >
            {isUploading ? <Loader2 size={16} className="spin" /> : <Upload size={16} />}
            <span>{isUploading ? 'Ingesting...' : 'Upload'}</span>
          </Button>
          <Button variant="secondary" className="section__header-btn" onClick={refreshDocuments}>
            <RefreshCw size={16} />
            <span>Refresh</span>
          </Button>
        </div>
      </div>

      {loading ? <Loader /> : null}

      <input
        ref={uploadInputRef}
        type="file"
        style={{ display: 'none' }}
        onChange={handleUploadFileChosen}
      />

      <input
        ref={fileInputRef}
        type="file"
        style={{ display: 'none' }}
        onChange={onReplaceFileChosen}
      />

      <div className="doc-list">
        {documents.length === 0 && !loading ? (
          <p className="section__subtitle">
            No documents yet. Upload a file from a chat to ingest it.
          </p>
        ) : null}

        {documents.map((doc, index) => (
          <motion.article
            key={doc.id}
            className="doc-row"
            initial={{ opacity: 0, y: 12 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ delay: index * 0.03 }}
          >
            <div>
              <p className="doc-row__title">
                {doc.name}
                {doc.versionCount > 1 ? (
                  <span className="doc-row__badge"> · v{doc.versionCount}</span>
                ) : null}
              </p>
              <p className="doc-row__meta">
                {fileExtension(doc.name)} · {formatBytes(doc.sizeBytes)} · {doc.chunks ?? 0} chunks
                {doc.ingestedAt ? ` · Added ${dayjs(doc.ingestedAt).format('MMM D, HH:mm')}` : ''}
              </p>
            </div>
            <div className="doc-row__actions">
              <Button
                variant="secondary"
                disabled={busyId === doc.id}
                onClick={() => openReplacePicker(doc.id)}
              >
                <RotateCcw size={16} />
                <span>Replace</span>
              </Button>
              <Button
                variant="danger"
                disabled={busyId === doc.id}
                onClick={() => onDelete(doc)}
              >
                <Trash2 size={16} />
                <span>Delete</span>
              </Button>
            </div>
          </motion.article>
        ))}
      </div>
    </section>
  )
}
