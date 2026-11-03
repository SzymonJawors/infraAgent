import { useEffect, useRef, useState, type FormEvent } from 'react'
import ReactMarkdown from 'react-markdown'
import type { AgentProgress, AgentTiming, ChatMessage } from '../lib/types'

type ChatPanelProps = {
  messages: ChatMessage[]
  message: string
  busy: string | null
  error: string
  progress: AgentProgress[]
  timing: AgentTiming[]
  onMessageChange: (value: string) => void
  onSubmit: (event: FormEvent) => void
  onCancel: () => void
}

export function ChatPanel({
  messages,
  message,
  busy,
  error,
  progress,
  timing,
  onMessageChange,
  onSubmit,
  onCancel,
}: ChatPanelProps) {
  const feedEndRef = useRef<HTMLDivElement>(null)
  const [timingExpanded, setTimingExpanded] = useState(false)

  useEffect(() => {
    feedEndRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' })
  }, [messages, busy, progress])

  return (
    <section className="chat-panel">
      <div className="chat-feed">
        {messages.map((item) => (
          <div className={`message-row ${item.role}`} key={item.id}>
            {item.role !== 'user' && <div className="avatar">{item.role === 'error' ? '!' : 'IA'}</div>}
            <div className="message-bubble">
              {item.role === 'user' ? item.text : <ReactMarkdown>{item.text}</ReactMarkdown>}
            </div>
          </div>
        ))}
        {(busy === 'message' || busy === 'cancelling') && (
          <div className="message-row">
            <div className="avatar">IA</div>
            <div className="message-bubble typing">
              <div className="typing-header">
                <div className="typing-status">
                  <span className="typing-label">{progress.at(-1)?.message ?? 'Working on your infrastructure request...'}</span>
                  {progress.at(-1)?.command && <code>{progress.at(-1)?.command}</code>}
                </div>
                <button
                  type="button"
                  className="cancel-job-button"
                  onClick={onCancel}
                  disabled={busy === 'cancelling'}
                  aria-label={busy === 'cancelling' ? 'Cancelling agent' : 'Stop agent'}
                  title={busy === 'cancelling' ? 'Cancelling agent' : 'Stop agent'}
                >
                  <span aria-hidden="true">■</span>
                </button>
              </div>
              <div className="typing-dots">
                <i />
                <i />
                <i />
              </div>
            </div>
          </div>
        )}
        <div ref={feedEndRef} aria-hidden="true" />
      </div>
      {timing.length > 0 && (
        <div className="timing-card">
          <button
            type="button"
            className="timing-toggle"
            onClick={() => setTimingExpanded((expanded) => !expanded)}
            aria-expanded={timingExpanded}
          >
            <span className="timing-card-title">Request timing</span>
            <span>{timingExpanded ? 'Hide' : 'Show'} timing</span>
          </button>
          {timingExpanded && (
            <div className="timing-grid">
              {timing.map((item) => (
                <div className="timing-item" key={`${item.label}-${item.milliseconds}`}>
                  <span>{item.label}</span>
                  <strong>{formatMilliseconds(item.milliseconds)}</strong>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
      <form className="composer" onSubmit={onSubmit}>
        <textarea
          value={message}
          onChange={(event) => onMessageChange(event.target.value)}
          placeholder="e.g. Create a private S3 bucket in eu-central-1..."
          rows={3}
          disabled={Boolean(busy)}
        />
        <div className="composer-footer">
          <span>Enter to send · Terraform changes require approval</span>
          <button className="send-button" type="submit" disabled={!message.trim() || Boolean(busy)}>
            Send <span>↗</span>
          </button>
        </div>
      </form>
      {error && <div className="error-banner">⚠ {error}</div>}
    </section>
  )
}

function formatMilliseconds(milliseconds: number): string {
  return milliseconds >= 1000
    ? `${(milliseconds / 1000).toFixed(1)} s`
    : `${milliseconds} ms`
}
