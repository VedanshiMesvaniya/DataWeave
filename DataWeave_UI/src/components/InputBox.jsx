import { forwardRef } from 'react'
import { ArrowUp, Loader2, Mic, Square } from 'lucide-react'
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
    getMicWaveform = null,
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
        <VoiceWaveform getWaveform={getMicWaveform} active />
      ) : (
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
      )}
      <div className="composer__footer">
        <div className="composer__footer-left">{footer}</div>
        <div className="composer__actions">
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
