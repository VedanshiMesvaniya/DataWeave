import { forwardRef } from 'react'
import { ArrowUp, Loader2, Mic, Square, X } from 'lucide-react'
import TextareaAutosize from 'react-textarea-autosize'
import VoiceWaveform from './VoiceWaveform.jsx'

const InputBox = forwardRef(function InputBox(
  {
    value,
    onChange,
    onSubmit,
    onStop,
    disabled = false,
    loading = false,
    placeholder = 'Write a message...',
    footer = null,
    micStatus = 'idle',
    onMicClick = null,
    onMicCancel = null,
    getMicWaveform = null,
    isRevealingVoice = false,
  },
  ref,
) {
  const canSubmit = value.trim().length > 0 && !disabled && !loading
  const micDisabled = disabled || loading || micStatus === 'transcribing'
  const isRecording = micStatus === 'recording'

  return (
    <form
      className="composer__shell"
      onSubmit={(event) => {
        event.preventDefault()
        if (canSubmit) onSubmit?.()
      }}
    >
      {isRecording ? (
        <div className="composer__listening">
          <span className="composer__listening-icon" aria-hidden="true">
            <span />
            <span />
            <span />
          </span>
          <span className="composer__listening-label">Listening...</span>
          <VoiceWaveform getWaveform={getMicWaveform} active />
        </div>
      ) : (
        <div className="composer__input-row">
          {isRevealingVoice ? (
            <Loader2 size={14} className="spin composer__input-row-icon" aria-hidden="true" />
          ) : null}
          <TextareaAutosize
            ref={ref}
            className="composer__input"
            placeholder={placeholder}
            value={value}
            onChange={(event) => onChange(event.target.value)}
            onKeyDown={(event) => {
              if (event.key !== 'Enter') return
              if (event.shiftKey) return
              event.preventDefault()
              if (canSubmit) onSubmit?.()
            }}
            minRows={1}
            maxRows={5}
            disabled={disabled || loading}
          />
        </div>
      )}
      <div className="composer__footer">
        <div className="composer__footer-left">{footer}</div>
        <div className="composer__actions">
          {isRecording && onMicCancel ? (
            <button
              type="button"
              className="composer__mic-cancel"
              onClick={onMicCancel}
              aria-label="Cancel recording"
              title="Cancel recording"
            >
              <X size={16} />
            </button>
          ) : null}
          {onMicClick ? (
            <button
              type="button"
              className="composer__mic"
              data-active={isRecording ? 'true' : 'false'}
              onClick={onMicClick}
              disabled={micDisabled}
              aria-label={isRecording ? 'Stop recording' : 'Start voice input'}
              aria-pressed={isRecording}
              title={isRecording ? 'Stop recording' : 'Voice input'}
            >
              {micStatus === 'transcribing' ? (
                <Loader2 size={16} className="spin" />
              ) : isRecording ? (
                <Square size={14} fill="currentColor" />
              ) : (
                <Mic size={16} />
              )}
            </button>
          ) : null}
          {loading ? (
            <button
              type="button"
              className="composer__stop"
              onClick={onStop}
              aria-label="Stop generating"
            >
              <Square size={14} fill="currentColor" />
            </button>
          ) : (
            <button
              type="submit"
              className="composer__send"
              disabled={!canSubmit}
              aria-label="Send message"
            >
              <ArrowUp size={18} strokeWidth={2.5} />
            </button>
          )}
        </div>
      </div>
    </form>
  )
})

export default InputBox
