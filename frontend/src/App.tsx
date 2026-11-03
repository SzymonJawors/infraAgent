import { useCallback, useEffect, useMemo, useState, type FormEvent } from 'react'
import './App.css'
import { ChatPanel } from './components/ChatPanel'
import { Inspector } from './components/Inspector'
import { Sidebar } from './components/Sidebar'
import { StatusDot } from './components/StatusDot'
import { cancelMessageWebSocket, createSessionId, getSessionId, request, streamMessageWebSocket } from './lib/api'
import type { AgentJobStatus, AgentProgress, AgentTiming, ChatMessage, CommandResponse, MessageResponse, OperationRecord, SessionSummary, WorkspaceFile, SessionStatus } from './lib/types'

function App() {
  const [sessionId] = useState(getSessionId)
  const [sessions, setSessions] = useState<SessionSummary[]>(() => {
    const now = new Date().toISOString()
    return [{ id: getSessionId(), title: 'Untitled environment', created_at: now, updated_at: now }]
  })
  const [message, setMessage] = useState('')
  const [messages, setMessages] = useState<ChatMessage[]>(() => getStoredMessages(sessionId))
  const [files, setFiles] = useState<WorkspaceFile[]>([])
  const [sessionStatus, setSessionStatus] = useState<SessionStatus | null>(null)
  const [selectedFile, setSelectedFile] = useState<string | null>(null)
  const [checks, setChecks] = useState<MessageResponse | null>(null)
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState('')
  const [lastCommand, setLastCommand] = useState<CommandResponse | null>(null)
  const [progress, setProgress] = useState<AgentProgress[]>([])
  const [timing, setTiming] = useState<AgentTiming[]>([])
  const [operations, setOperations] = useState<OperationRecord[]>([])
  const [confirmation, setConfirmation] = useState<{
    title: string
    message: string
    confirmLabel: string
    danger?: boolean
    onConfirm: () => void
  } | null>(null)

  const appendAssistantResponse = useCallback((result: MessageResponse) => {
    const marker = `infraagent-job:${sessionId}`
    const responseKey = JSON.stringify(result)
    if (localStorage.getItem(marker) === responseKey) return false
    localStorage.setItem(marker, responseKey)
    setChecks(result)
    setMessages((current) => [...current, { id: Date.now(), role: 'assistant', text: result.reply }])
    return true
  }, [sessionId])

  const selectedContent = useMemo(
    () => files.find((file) => file.name === selectedFile)?.content ?? '',
    [files, selectedFile],
  )

  const loadFiles = useCallback(async () => {
    const result = await request<{ files: WorkspaceFile[] }>(`/sessions/${sessionId}/files`)
    setFiles(result.files)
    if (result.files[0]) setSelectedFile((current) => current ?? result.files[0].name)
  }, [sessionId])

  const loadSessions = useCallback(async () => {
    const result = await request<{ sessions: SessionSummary[] }>('/sessions')
    setSessions((_current) => {
      if (result.sessions.some((session) => session.id === sessionId)) return result.sessions
      return [
        {
          id: sessionId,
          title: 'Untitled environment',
          created_at: new Date().toISOString(),
          updated_at: new Date().toISOString(),
        },
        ...result.sessions,
      ]
    })
  }, [sessionId])

  const loadSessionStatus = useCallback(async () => {
    const result = await request<SessionStatus>(`/sessions/${sessionId}/status`)
    setSessionStatus(result)
  }, [sessionId])

  const loadOperations = useCallback(async () => {
    const result = await request<{ operations: OperationRecord[] }>(`/sessions/${sessionId}/operations`)
    setOperations(result.operations)
  }, [sessionId])

  useEffect(() => {
    void (async () => {
      try {
        await loadFiles()
        await loadSessions()
      } catch {
        setError('Could not load session history.')
        setSessions((current) =>
          current.some((session) => session.id === sessionId)
            ? current
            : [{
                id: sessionId,
                title: 'Untitled environment',
                created_at: new Date().toISOString(),
                updated_at: new Date().toISOString(),
              }, ...current],
        )
      }
    })()
  }, [loadFiles, loadSessions])

  useEffect(() => {
    void loadSessionStatus().catch(() => setError('Could not load infrastructure status.'))
  }, [loadSessionStatus])
  useEffect(() => {
    void loadOperations().catch(() => setError('Could not load operation history.'))
  }, [loadOperations])

  const restoreJob = useCallback(async () => {
    const result = await request<AgentJobStatus>(`/sessions/${sessionId}/agent-status`)
    if (result.status === 'running') {
      setBusy('message')
      setProgress(result.progress)
      const metricEvent = result.progress.find((item) => item.stage === 'metrics')
      if (metricEvent) setTiming(parseTiming(metricEvent.message))
      return
    }
    if ((result.status === 'failed' || result.status === 'cancelled') && result.error) {
      setBusy(null)
      if (!result.error.toLowerCase().includes('busy')) setError(result.error)
      return
    }
    if (result.status === 'completed' && result.result) {
      if (appendAssistantResponse(result.result)) {
        await loadFiles()
        await loadSessions()
        await loadSessionStatus()
      }
      const metricEvent = result.progress.find((item) => item.stage === 'metrics')
      if (metricEvent) setTiming(parseTiming(metricEvent.message))
      setBusy(null)
    }
  }, [appendAssistantResponse, loadFiles, loadSessionStatus, loadSessions])

  useEffect(() => {
    void restoreJob().catch(() => setError('Could not load agent status.'))
    const timer = window.setInterval(() => {
      void restoreJob().catch(() => undefined)
    }, 2000)
    return () => window.clearInterval(timer)
  }, [restoreJob])

  useEffect(() => {
    localStorage.setItem(`infraagent-messages:${sessionId}`, JSON.stringify(messages))
  }, [messages, sessionId])

  async function sendMessage(event: FormEvent) {
    event.preventDefault()
    const trimmed = message.trim()
    if (!trimmed || busy) return
    setMessage('')
    setError('')
    setLastCommand(null)
    setProgress([])
    setTiming([])
    localStorage.removeItem(`infraagent-job:${sessionId}`)
    setMessages((current) => [...current, { id: Date.now(), role: 'user', text: trimmed }])
    setBusy('message')
    try {
      const result = await streamMessageWebSocket(
        sessionId,
        { message: trimmed },
        (item) => {
          setProgress((current) => [...current, item])
          if (item.stage === 'metrics') setTiming(parseTiming(item.message))
        },
      )
      appendAssistantResponse(result)
      await loadOperations()
      await loadFiles()
      await loadSessions()
      await loadSessionStatus()
    } catch (requestError) {
      const text = requestError instanceof Error ? requestError.message : 'Could not connect to the API.'
      setError(text)
      setMessages((current) => [...current, { id: Date.now() + 1, role: 'error', text }])
    } finally {
      setBusy(null)
    }

  }

  async function cancelAgent() {
    if (busy === 'cancelling') return
    setBusy('cancelling')
    cancelMessageWebSocket(sessionId)
    try {
      await request(`/sessions/${sessionId}/agent-cancel`, { method: 'POST' })
      await loadOperations()
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : 'Could not cancel the agent.')
    }
  }

  async function executeAction(path: string, label: string) {
    setBusy(label)
    setError('')
    setLastCommand(null)
    try {
      const result = await request<CommandResponse>(`/sessions/${sessionId}${path}`, {
        method: 'POST',
        body: path.includes('/plans/') ? undefined : JSON.stringify({ approve: true }),
      })
      setLastCommand(result)
      await loadSessionStatus()
      await loadOperations()
      if (path === '/destroy') {
        setMessages((current) => [
          ...current,
          {
            id: Date.now(),
            role: 'assistant',
            text:
              result.status === 'no_managed_resources'
                ? 'No managed Terraform resources were found.'
                : result.ok
                  ? 'Destroy completed successfully. The managed AWS resources have been removed.'
                  : 'The destroy operation failed. Review the command output.',
          },
        ])
      }
      if (path.includes('/plans/destroy')) {
        setMessages((current) => [
          ...current,
          {
            id: Date.now(),
            role: 'assistant',
            text:
              result.status === 'no_managed_resources'
                ? 'There are no Terraform-managed resources to destroy. Apply the infrastructure first.'
                : result.ok
                  ? 'The destroy preview is ready. Review the resources in the inspector, then approve the destroy operation.'
                  : 'The destroy plan failed.',
          },
        ])
      }

    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : 'The Terraform action failed.')
    } finally {
      setBusy(null)
    }
  }

  function runAction(path: string, label: string) {
    if (path === '/apply' || path === '/destroy') {
      setConfirmation({
        title: path === '/apply' ? 'Approve infrastructure deployment' : 'Approve infrastructure destroy',
        message:
          path === '/apply'
            ? 'Terraform will create or change AWS resources using the reviewed plan.'
            : 'Terraform will permanently remove the managed AWS resources. This action cannot be undone.',
        confirmLabel: path === '/apply' ? 'Approve & apply' : 'Destroy resources',
        danger: path === '/destroy',
        onConfirm: () => {
          setConfirmation(null)
          void executeAction(path, label)
        },
      })
      return
    }
    void executeAction(path, label)
  }

  async function deleteSession(id: string) {
    const target = sessions.find((session) => session.id === id)
    if (!target) return
    setConfirmation({
      title: 'Delete session',
      message: `Delete "${target.title}"? This removes its Terraform files and cannot be undone.`,
      confirmLabel: 'Delete session',
      danger: true,
      onConfirm: () => {
        setConfirmation(null)
        void deleteSessionConfirmed(id)
      },
    })
  }

  async function deleteSessionConfirmed(id: string) {
    const target = sessions.find((session) => session.id === id)
    if (!target) return
    try {
      await request<void>(`/sessions/${id}`, { method: 'DELETE' })
      localStorage.removeItem(`infraagent-messages:${id}`)
      localStorage.removeItem(`infraagent-job:${id}`)
      const remaining = await request<{ sessions: SessionSummary[] }>('/sessions')
      if (id === sessionId) {
        const nextSession = remaining.sessions[0]?.id ?? createSessionId()
        localStorage.setItem('infraagent-session', nextSession)
        window.location.reload()
        return
      }
      setSessions(remaining.sessions)
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : 'Could not delete the session.')
    }
  }

  async function renameSession(id: string, title: string) {
    try {
      await request(`/sessions/${id}`, {
        method: 'PATCH',
        body: JSON.stringify({ title }),
      })
      await loadSessions()
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : 'Could not rename the session.')
      throw requestError
    }
  }

  return (
    <div className="app-shell">
      <header className="topbar">
        <div className="brand">
          <div className="brand-mark">IA</div>
          <div>
            <strong>InfraAgent</strong>
            <span>Infrastructure workspace</span>
          </div>
        </div>
        <div className="topbar-meta">
          <span className="connection">
            <StatusDot ok /> API connected
          </span>
          <span className="session-pill">
            <span>SESSION</span>
            {sessionId}
          </span>
          <button
            className="new-session-button"
            type="button"
            onClick={() => {
              const nextSession = createSessionId()
              localStorage.setItem('infraagent-session', nextSession)
              window.location.reload()
            }}
          >
            New session
          </button>
        </div>
      </header>
      <main className="workspace-grid">
        <Sidebar
          files={files}
          selectedFile={selectedFile}
          onSelectFile={setSelectedFile}
          sessions={sessions}
          sessionId={sessionId}
          onSelectSession={(id) => {
            localStorage.setItem('infraagent-session', id)
            window.location.reload()
          }}
          onDeleteSession={(id) => void deleteSession(id)}
          onRenameSession={renameSession}
          operations={operations}
        />
        <ChatPanel
          messages={messages}
          message={message}
          busy={busy}
          checksReady={Boolean(checks)}
          error={error}
          progress={progress}
          timing={timing}
          onMessageChange={setMessage}
          onSubmit={sendMessage}
          onCancel={() => void cancelAgent()}
        />
        <Inspector
          checks={checks}
          files={files}
          selectedFile={selectedFile}
          selectedContent={selectedContent}
          busy={busy}
          lastCommand={lastCommand}
          onAction={runAction}
          changes={checks?.changes ?? []}
          sessionStatus={sessionStatus}
        />
      </main>
      {confirmation && (
        <div className="modal-backdrop" role="presentation" onMouseDown={(event) => {
          if (event.target === event.currentTarget) setConfirmation(null)
        }}>
          <section className={`confirm-modal ${confirmation.danger ? 'danger' : ''}`} role="dialog" aria-modal="true" aria-labelledby="confirm-title">
            <div className="confirm-icon">{confirmation.danger ? '!' : '✓'}</div>
            <span className="eyebrow">Confirmation required</span>
            <h2 id="confirm-title">{confirmation.title}</h2>
            <p>{confirmation.message}</p>
            <div className="confirm-actions">
              <button type="button" className="modal-cancel" onClick={() => setConfirmation(null)}>Cancel</button>
              <button type="button" className={confirmation.danger ? 'modal-danger' : 'modal-confirm'} onClick={confirmation.onConfirm}>
                {confirmation.confirmLabel}
              </button>
            </div>
          </section>
        </div>
      )}
    </div>
  )
}

function parseTiming(message: string): AgentTiming[] {
  const values: AgentTiming[] = []
  for (const match of message.matchAll(/([a-z0-9_]+)\s+(\d+)\s*ms/gi)) {
    values.push({
      label: match[1].replace(/_/g, ' '),
      milliseconds: Number(match[2]),
    })
  }
  return values
}

function getStoredMessages(sessionId: string): ChatMessage[] {
  const fallback: ChatMessage[] = [
    {
      id: 1,
      role: 'assistant',
      text: 'Describe the infrastructure you need. I will generate Terraform, validate it, and show you the plan before deployment.',
    },
  ]
  const stored = localStorage.getItem(`infraagent-messages:${sessionId}`)
  if (!stored) return fallback
  try {
    const messages = JSON.parse(stored) as ChatMessage[]
    return Array.isArray(messages) && messages.length > 0 ? messages : fallback
  } catch {
    return fallback
  }
}

export default App
