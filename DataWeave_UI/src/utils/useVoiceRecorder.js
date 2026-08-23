import { useCallback, useRef, useState } from 'react'
import { transcribeAudio } from '../services/api.js'

// Time-domain buffer length is fftSize (not fftSize/2 like frequency data) —
// 256 gives a smooth trace without being expensive to read every frame.
const FFT_SIZE = 256

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
 *   startRecording / stopRecording / toggleRecording — stop finishes the
 *                    recording and transcribes it.
 *   cancelRecording() — stops and discards the recording without
 *                    transcribing anything (no upload happens at all).
 *   getWaveform() -> number[] (each -1..1) — the mic's actual waveform
 *                    shape for this animation frame (an oscilloscope-style
 *                    trace, not just an overall volume level), so a line
 *                    drawn from it genuinely moves up and down with voice
 *                    pitch. Read this from your own requestAnimationFrame
 *                    loop while status === 'recording' rather than storing
 *                    it in React state — it changes every frame.
 */
export function useVoiceRecorder({ onTranscript, onError } = {}) {
  const [status, setStatus] = useState('idle')
  const [error, setError] = useState(null)
  const mediaRecorderRef = useRef(null)
  const chunksRef = useRef([])
  const streamRef = useRef(null)
  const audioContextRef = useRef(null)
  const analyserRef = useRef(null)
  const timeDataRef = useRef(null)
  // Set right before calling recorder.stop() from cancelRecording(), so the
  // async onstop handler knows to throw the clip away instead of uploading
  // it. Not React state — it only needs to be read once, synchronously,
  // inside onstop.
  const cancelledRef = useRef(false)

  const cleanupStream = useCallback(() => {
    streamRef.current?.getTracks().forEach((track) => track.stop())
    streamRef.current = null
  }, [])

  const cleanupAudioAnalysis = useCallback(() => {
    analyserRef.current = null
    timeDataRef.current = null
    const ctx = audioContextRef.current
    audioContextRef.current = null
    if (ctx && ctx.state !== 'closed') {
      ctx.close().catch(() => {})
    }
  }, [])

  // Polled by the waveform UI's own animation loop, not by React state —
  // this can be sampled 30-60x/sec and shouldn't trigger a component
  // re-render that often. Returns [] when there's nothing to show (not
  // recording, or the analyser failed to set up — the waveform is purely
  // cosmetic and never blocks recording/transcription).
  const getWaveform = useCallback(() => {
    const analyser = analyserRef.current
    const data = timeDataRef.current
    if (!analyser || !data) return []

    analyser.getByteTimeDomainData(data)
    // Raw bytes are 0..255 centered on 128 (silence); normalize to -1..1 so
    // the UI can map it straight onto a "how far above/below center" line.
    const samples = new Array(data.length)
    for (let i = 0; i < data.length; i++) {
      samples[i] = (data[i] - 128) / 128
    }
    return samples
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
      // getByteTimeDomainData reads the current frame's samples and nothing
      // more, discarded again on the very next call.
      try {
        const AudioContextCtor = window.AudioContext || window.webkitAudioContext
        const audioContext = new AudioContextCtor()
        const source = audioContext.createMediaStreamSource(stream)
        const analyser = audioContext.createAnalyser()
        analyser.fftSize = FFT_SIZE
        source.connect(analyser)
        audioContextRef.current = audioContext
        analyserRef.current = analyser
        timeDataRef.current = new Uint8Array(analyser.fftSize)
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

        if (cancelledRef.current) {
          // Discarded by cancelRecording() — no transcription, no upload.
          // `blob` above is a local variable that falls out of scope right
          // here; nothing in the app ever gets a reference to it.
          cancelledRef.current = false
          setStatus('idle')
          return
        }

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

  const cancelRecording = useCallback(() => {
    if (status !== 'recording') return
    cancelledRef.current = true
    mediaRecorderRef.current?.stop()
  }, [status])

  const toggleRecording = useCallback(() => {
    if (status === 'recording') {
      stopRecording()
    } else if (status === 'idle') {
      startRecording()
    }
  }, [status, startRecording, stopRecording])

  return {
    status,
    error,
    startRecording,
    stopRecording,
    cancelRecording,
    toggleRecording,
    getWaveform,
  }
}
