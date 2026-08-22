import { useCallback, useRef, useState } from 'react'
import { transcribeAudio } from '../services/api.js'

/**
 * Browser-mic voice input for the chat composer.
 *
 * Flow: click mic -> browser records via MediaRecorder -> click mic again (or
 * it stops) -> the recorded clip is sent to POST /api/transcribe -> the
 * returned text is handed to `onTranscript` so the caller can drop it into
 * the existing input box, exactly like typed text. The RAG pipeline never
 * knows whether a question came from the keyboard or the microphone.
 *
 * Returns:
 *   status        'idle' | 'recording' | 'transcribing'
 *   error         string | null — last error message, if any
 *   startRecording / stopRecording / toggleRecording
 */
export function useVoiceRecorder({ onTranscript, onError } = {}) {
  const [status, setStatus] = useState('idle')
  const [error, setError] = useState(null)
  const mediaRecorderRef = useRef(null)
  const chunksRef = useRef([])
  const streamRef = useRef(null)

  const cleanupStream = useCallback(() => {
    streamRef.current?.getTracks().forEach((track) => track.stop())
    streamRef.current = null
  }, [])

  const startRecording = useCallback(async () => {
    if (status !== 'idle') return
    setError(null)

    if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === 'undefined') {
      const message = 'Voice input is not supported in this browser.'
      setError(message)
      onError?.(message)
      return
    }

    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true })
      streamRef.current = stream

      const mimeType = MediaRecorder.isTypeSupported('audio/webm')
        ? 'audio/webm'
        : MediaRecorder.isTypeSupported('audio/ogg')
          ? 'audio/ogg'
          : ''

      const recorder = mimeType ? new MediaRecorder(stream, { mimeType }) : new MediaRecorder(stream)
      chunksRef.current = []

      recorder.ondataavailable = (event) => {
        if (event.data && event.data.size > 0) chunksRef.current.push(event.data)
      }

      recorder.onstop = async () => {
        cleanupStream()
        const blob = new Blob(chunksRef.current, { type: recorder.mimeType || 'audio/webm' })
        chunksRef.current = []

        if (blob.size === 0) {
          setStatus('idle')
          return
        }

        setStatus('transcribing')
        try {
          const { text } = await transcribeAudio(blob)
          if (text && text.trim()) {
            onTranscript?.(text.trim())
          }
        } catch (err) {
          const message =
            err?.response?.data?.detail || 'Could not transcribe audio. Please try again.'
          setError(message)
          onError?.(message)
        } finally {
          setStatus('idle')
        }
      }

      mediaRecorderRef.current = recorder
      recorder.start()
      setStatus('recording')
    } catch {
      const message = 'Microphone access was denied or is unavailable.'
      setError(message)
      onError?.(message)
      cleanupStream()
      setStatus('idle')
    }
  }, [status, onTranscript, onError, cleanupStream])

  const stopRecording = useCallback(() => {
    if (status !== 'recording') return
    mediaRecorderRef.current?.stop()
  }, [status])

  const toggleRecording = useCallback(() => {
    if (status === 'recording') {
      stopRecording()
    } else if (status === 'idle') {
      startRecording()
    }
  }, [status, startRecording, stopRecording])

  return { status, error, startRecording, stopRecording, toggleRecording }
}
