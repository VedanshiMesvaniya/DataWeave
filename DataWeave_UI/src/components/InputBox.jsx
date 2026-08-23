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
    getMicLevels = null,
  },
  ref,
) {
  const canSubmit = value.trim().length > 0 && !disabled && !loading
  const micDisabled = disabled || loading || micStatus === 'transcribing'

  return (
    <form
      className="composer__shell"
      onSubmit={(event) => {
        event.preventDefault()
        if (canSubmit) onSubmit?.()
      }}
    >
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
      <div className="composer__footer">
        <div className="composer__footer-left">{footer}</div>
        <div className="composer__actions">
          {onMicClick ? (
            <div className="composer__mic-group">
              <VoiceWaveform getLevels={getMicLevels} active={micStatus === 'recording'} />
              <button
                type="button"
                className="composer__mic"
                data-active={micStatus === 'recording' ? 'true' : 'false'}
                onClick={onMicClick}
                disabled={micDisabled}
                aria-label={micStatus === 'recording' ? 'Stop recording' : 'Start voice input'}
                aria-pressed={micStatus === 'recording'}
                title={micStatus === 'recording' ? 'Stop recording' : 'Voice input'}
              >
                {micStatus === 'transcribing' ? (
                  <Loader2 size={16} className="spin" />
                ) : (
                  <Mic size={16} />
                )}
              </button>
            </div>
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
