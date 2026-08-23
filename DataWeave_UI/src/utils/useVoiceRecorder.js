import { useCallback, useRef, useState } from 'react'
import { transcribeAudio } from '../services/api.js'

// Small FFT — we only need a handful of bars for the waveform, not a
// full spectrum analysis.
const FFT_SIZE = 64
const WAVEFORM_BARS = 5

/**
 * Browser-mic voice input for the chat composer.
 *
 * Flow: click mic -> browser records via MediaRecorder (with a live Web Audio
 * waveform for visual feedback, never played back or stored) -> click mic
 * again (or it stops) -> the recorded clip is sent to POST /api/transcribe ->
 * the returned text is handed to `onTranscript` so the caller can drop it
 * into the existing input box, exactly like typed text.
 *
 * The RAG pipeline never knows whether a question came from the keyboard or
 * the microphone, and the audio itself never outlives the transcription
 * request: the recorded Blob is a local variable that's discarded the moment
 * transcribeAudio() resolves (nothing in the app holds a reference to it
 * afterwards), and the server deletes its temp-file copy in a `finally`
 * block the instant transcription finishes (or fails) — see
 * src/api/speech.py. No audio is ever written to durable storage on either
 * side.
 *
 * Returns:
 *   status        'idle' | 'recording' | 'transcribing'
 *   error         string | null — last error message, if any
 *   startRecording / stopRecording / toggleRecording
 *   getLevels()   -> number[] of length WAVEFORM_BARS, each 0..1 — read this
 *                    from your own requestAnimationFrame loop while
 *                    status === 'recording' to drive a waveform without
 *                    forcing React re-renders on every audio frame.
 */
export function useVoiceRecorder({ onTranscript, onError } = {}) {
  const [status, setStatus] = useState('idle')
  const [error, setError] = useState(null)
  const mediaRecorderRef = useRef(null)
  const chunksRef = useRef([])
  const streamRef = useRef(null)
  const audioContextRef = useRef(null)
  const analyserRef = useRef(null)
  const freqDataRef = useRef(null)

  const cleanupStream = useCallback(() => {
    streamRef.current?.getTracks().forEach((track) => track.stop())
    streamRef.current = null
  }, [])

  const cleanupAudioAnalysis = useCallback(() => {
    analyserRef.current = null
    freqDataRef.current = null
    const ctx = audioContextRef.current
    audioContextRef.current = null
    if (ctx && ctx.state !== 'closed') {
      ctx.close().catch(() => {})
    }
  }, [])

  // Polled by the waveform UI's own animation loop, not by React state — a
  // mic-level readout can change 30-60x/sec and shouldn't trigger a
  // component re-render that often. Returns [] when there's nothing to show
  // (not recording, or the analyser failed to set up — waveform is purely
  // cosmetic and never blocks recording/transcription).
  const getLevels = useCallback(() => {
    const analyser = analyserRef.current
    const data = freqDataRef.current
    if (!analyser || !data) return []

    analyser.getByteFrequencyData(data)
    const bucketSize = Math.max(1, Math.floor(data.length / WAVEFORM_BARS))
    const levels = []
    for (let i = 0; i < WAVEFORM_BARS; i++) {
      let sum = 0
      for (let j = 0; j < bucketSize; j++) {
        sum += data[i * bucketSize + j] || 0
      }
      levels.push(Math.min(1, sum / bucketSize / 255))
    }
    return levels
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

      // Live waveform: analyse the raw mic signal for visual feedback only.
      // The analyser is never connected to audioContext.destination, so
      // nothing is played back, and no audio data is retained by it —
      // getByteFrequencyData reads the current frame and nothing more.
      try {
        const AudioContextCtor = window.AudioContext || window.webkitAudioContext
        const audioContext = new AudioContextCtor()
        const source = audioContext.createMediaStreamSource(stream)
        const analyser = audioContext.createAnalyser()
        analyser.fftSize = FFT_SIZE
        source.connect(analyser)
        audioContextRef.current = audioContext
        analyserRef.current = analyser
        freqDataRef.current = new Uint8Array(analyser.frequencyBinCount)
      } catch {
        // Waveform is a nice-to-have; recording/transcription work without it.
      }

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
        cleanupAudioAnalysis()

        const blob = new Blob(chunksRef.current, { type: recorder.mimeType || 'audio/webm' })
        // Drop the only other reference to the raw chunks immediately —
        // once `blob` above is built, this array is redundant, and clearing
        // it means the recorded audio bytes exist in exactly one place
        // (the local `blob`) for exactly as long as the upload takes.
        chunksRef.current = []
        mediaRecorderRef.current = null

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
          // `blob` goes out of scope here — nothing in the app references
          // the recorded audio anymore, so it's immediately eligible for
          // garbage collection. The server has already deleted its own
          // temp-file copy by this point too (src/api/speech.py's `finally`
          // block runs before the response is returned), so no copy of the
          // recording survives past this point on either side.
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
      cleanupAudioAnalysis()
      setStatus('idle')
    }
  }, [status, onTranscript, onError, cleanupStream, cleanupAudioAnalysis])

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

  return { status, error, startRecording, stopRecording, toggleRecording, getLevels }
}
