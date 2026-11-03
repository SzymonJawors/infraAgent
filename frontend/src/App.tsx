import { useCallback, useEffect, useMemo, useState, type FormEvent } from 'react'
import './App.css'
import { ChatPanel } from './components/ChatPanel'
import { Inspector } from './components/Inspector'
import { Sidebar } from './components/Sidebar'
import { StatusDot } from './components/StatusDot'
import { cancelMessageWebSocket, createSessionId, downloadProject, getSessionId, request, streamMessageWebSocket } from './lib/api'
import type { AgentJobStatus, AgentProgress, AgentTiming, ChatMessage, CommandResponse, ConversationMessage, GithubAnalysis, GithubRepository, MessageResponse, OperationRecord, SessionSummary, WorkspaceFile, SessionStatus } from './lib/types'

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
  const [selectedOperation, setSelectedOperation] = useState<OperationRecord | null>(null)
  const [githubConnected, setGithubConnected] = useState(false)
  const [githubRepositories, setGithubRepositories] = useState<GithubRepository[]>([])
  const [githubAnalysis, setGithubAnalysis] = useState<GithubAnalysis | null>(null)
  const [showGithub, setShowGithub] = useState(false)
  const [githubBusy, setGithubBusy] = useState(false)
  const [runCheckov, setRunCheckov] = useState(() => localStorage.getItem('infraagent-run-checkov') !== 'false')
  const [viewer, setViewer] = useState<{ title: string; content: string } | null>(null)
  const activeSession = sessions.find((session) => session.id === sessionId)
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
    const alreadyShown = localStorage.getItem(marker) === responseKey
    localStorage.setItem(marker, responseKey)
    setChecks(result)
    if (alreadyShown) return false
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
    const active = result.operations.find((operation) =>
      operation.status === 'running' && ['apply', 'destroy', 'destroy-plan'].includes(operation.kind),
    )
    if (active) {
      setBusy((current) => current ?? active.kind)
    }
  }, [sessionId])

  const waitForOperation = useCallback(async (operationId: number) => {
    for (;;) {
      const result = await request<{ operations: OperationRecord[] }>(`/sessions/${sessionId}/operations`)
      const operation = result.operations.find((item) => item.id === operationId)
      setOperations(result.operations)
      if (!operation || operation.status !== 'running') {
        return request<CommandResponse>(`/sessions/${sessionId}/operations/${operationId}`)
      }
      setLastCommand({
        ok: true,
        command: operation.command || `terraform ${operation.kind}`,
        output: operation.output || 'Terraform operation is still running.',
        status: 'running',
        operation_id: operationId,
      })
      await new Promise((resolve) => window.setTimeout(resolve, 750))
    }
  }, [sessionId])

  useEffect(() => {
    void (async () => {
      try {
        await request(`/sessions/${sessionId}`, { method: 'POST' })
        const conversation = await request<{ messages: ConversationMessage[] }>(
          `/sessions/${sessionId}/conversation`,
        )
        setMessages(
          conversation.messages.length > 0
            ? conversation.messages.map((item, index) => ({
                id: index + 1,
                role: item.role,
                text: item.content,
              }))
            : getWelcomeMessage(),
        )
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
  }, [loadFiles, loadSessions, sessionId])

  useEffect(() => {
    void loadSessionStatus().catch(() => setError('Could not load infrastructure status.'))
  }, [loadSessionStatus])
  useEffect(() => {
    void request<{ connected: boolean }>(`/sessions/${sessionId}/github/status`)
      .then((result) => setGithubConnected(result.connected))
      .catch(() => undefined)
    if (new URLSearchParams(window.location.search).get('github') === 'connected') {
      window.history.replaceState({}, '', window.location.pathname)
      setGithubConnected(true)
    }
  }, [sessionId])
  useEffect(() => {
    void loadOperations().catch(() => setError('Could not load operation history.'))
    const timer = window.setInterval(() => {
      void loadOperations().catch(() => undefined)
    }, 1000)
    return () => window.clearInterval(timer)
  }, [loadOperations])

  const restoreJob = useCallback(async () => {
    const result = await request<AgentJobStatus>(`/sessions/${sessionId}/agent-status`)
    if (result.status === 'running') {
      setBusy((current) => current && current !== 'message' ? current : 'message')
      setProgress(result.progress.filter((item) => item.stage !== 'metrics'))
      const metricEvent = result.progress.find((item) => item.stage === 'metrics')
      if (metricEvent) setTiming(parseTiming(metricEvent.message))
      return
    }
    if ((result.status === 'failed' || result.status === 'cancelled') && result.error) {
      setBusy((current) => current === 'message' ? null : current)
      setError(result.error)
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
      setBusy((current) => current === 'message' ? null : current)
    }
  }, [appendAssistantResponse, loadFiles, loadSessionStatus, loadSessions, sessionId])

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

  async function createNewSession() {
    const nextSession = createSessionId()
    try {
      await request(`/sessions/${nextSession}`, { method: 'POST' })
      localStorage.setItem('infraagent-session', nextSession)
      window.location.reload()
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : 'Could not create a new session.')
    }

  }

  async function openGithub() {
    if (!githubConnected) {
      const apiBase = import.meta.env.VITE_API_URL ?? 'http://127.0.0.1:8000'
      window.location.assign(`${apiBase}/github/auth?session_id=${encodeURIComponent(sessionId)}`)
      return
    }
    setGithubAnalysis(null)
    setShowGithub(true)
    setGithubBusy(true)
    try {
      const result = await request<GithubRepository[]>(`/sessions/${sessionId}/github/repositories`)
      setGithubRepositories(result)
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : 'Could not load GitHub repositories.')
    } finally {
      setGithubBusy(false)
    }
  }

  async function analyzeRepository(repository: GithubRepository) {
    setGithubBusy(true)
    try {
      const result = await request<GithubAnalysis>(`/sessions/${sessionId}/github/analyze`, {
        method: 'POST',
        body: JSON.stringify({
          full_name: repository.full_name,
          default_branch: repository.default_branch,
        }),
      })
      setGithubAnalysis(result)
      await loadSessions()
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : 'Could not analyze repository.')
    } finally {
      setGithubBusy(false)
    }
  }

  async function importRepository(repository: GithubAnalysis) {
    setGithubBusy(true)
    try {
      await request(`/sessions/${sessionId}/github/import`, {
        method: 'POST',
        body: JSON.stringify({ full_name: repository.repository, default_branch: repository.branch }),
      })
      await loadFiles()
      setShowGithub(false)
      setGithubAnalysis(null)
      setMessage(repository.suggested_prompt)
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : 'Could not import repository Terraform.')
    } finally {
      setGithubBusy(false)
    }
  }

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
        { message: trimmed, skip_checkov: !runCheckov },
        (item) => {
          if (item.stage === 'metrics') {
            setTiming(parseTiming(item.message))
            return
          }
          setProgress((current) => [...current, item])
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

  async function cancelOperation() {
    const operationId = lastCommand?.operation_id
    if (operationId == null) return
    try {
      await request(`/sessions/${sessionId}/operations/${operationId}/cancel`, { method: 'POST' })
      setLastCommand((current) => current ? {
        ...current,
        ok: false,
        status: 'cancelled',
        output: 'Terraform operation cancelled.',
      } : current)
      setMessages((current) => [
        ...current,
        { id: Date.now(), role: 'assistant', text: 'Operation cancelled. No further Terraform changes are being applied.' },
      ])
      await loadOperations()
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : 'Could not cancel Terraform operation.')
    }
  }

  async function executeAction(path: string, label: string) {
    setBusy(label)
    setError('')
    setLastCommand(null)
    try {
      let result = await request<CommandResponse>(`/sessions/${sessionId}${path}`, {
        method: 'POST',
        body: path.includes('/plans/') ? undefined : JSON.stringify({ approve: true }),
      })
      setLastCommand(result)
      if (result.operation_id != null) {
        if (result.status === 'running') {
          result = await waitForOperation(result.operation_id)
        }
      }
      setLastCommand(result)
      if (path === '/destroy') {
        setMessages((current) => [
          ...current,
          {
            id: Date.now(),
            role: result.status === 'cancelled' ? 'assistant' : result.ok ? 'assistant' : 'error',
            text:
              result.status === 'cancelled'
                ? 'Destroy operation cancelled.'
                : result.status === 'no_managed_resources'
                ? 'No managed Terraform resources were found.'
                : result.ok
                  ? 'Destroy completed successfully. The managed AWS resources have been removed.'
                  : 'The destroy operation failed. Review the command output.',
          },
        ])
      }
      if (path === '/apply') {
        setMessages((current) => [
          ...current,
          {
            id: Date.now(),
            role: result.status === 'cancelled' ? 'assistant' : result.ok ? 'assistant' : 'error',
            text: result.status === 'cancelled'
              ? 'Apply operation cancelled. No further Terraform changes are being applied.'
              : result.ok
              ? 'Apply completed successfully. The AWS infrastructure is now deployed.'
              : 'Apply failed. Open the full Terraform output in the Inspector to see the exact error.',
          },
        ])
      }
      if (path.includes('/plans/apply')) {
        setMessages((current) => [
          ...current,
          {
            id: Date.now(),
            role: 'assistant',
            text: result.ok
              ? 'The apply plan is ready. Review it, then use Approve & apply to deploy the infrastructure.'
              : 'The apply plan could not be loaded.',
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
              result.status === 'cancelled'
                ? 'Destroy preview cancelled.'
                : result.status === 'no_managed_resources'
                ? 'There are no Terraform-managed resources to destroy. Apply the infrastructure first.'
                : result.ok
                  ? 'The destroy preview is ready. Review the resources in the inspector, then approve the destroy operation.'
                  : 'The destroy plan failed.',
          },
        ])
      }
      await Promise.allSettled([loadSessionStatus(), loadOperations()])
    } catch (requestError) {
      const text = requestError instanceof Error ? requestError.message : 'The Terraform action failed.'
      setError(text)
      setMessages((current) => [
        ...current,
        { id: Date.now(), role: 'error', text: `Terraform operation failed: ${text}` },
      ])
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
            <span>
              Infrastructure workspace · Made by{' '}
              <a
                className="author-link"
                href="https://github.com/SzymonJawors"
                target="_blank"
                rel="noreferrer"
              >
                Szymon Jaworski
              </a>
            </span>
          </div>
        </div>
        <div className="product-heading">
          <span className="eyebrow">AI infrastructure assistant</span>
          <strong>Build with confidence.</strong>
          <span>Configuration, validation and security review.</span>
        </div>
        <div className="topbar-meta">
          <span className="heading-status">
            <StatusDot ok={Boolean(checks)} warning={!checks} /> {checks ? 'Ready for review' : 'Waiting for request'}
          </span>
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
              void createNewSession()
            }}
          >
            <span className="new-session-label">New session</span>
            <span className="new-session-short">New</span>
          </button>
          <button type="button" className="github-button" onClick={() => void openGithub()}>
            <span>◈</span>
            {githubConnected ? 'GitHub repositories' : 'Connect GitHub'}
          </button>
        </div>
      </header>
      <main className="workspace-grid">
        <Sidebar
          files={files}
          selectedFile={selectedFile}
          onSelectFile={setSelectedFile}
          onOpenFile={(name) => {
            const file = files.find((item) => item.name === name)
            if (file) setViewer({ title: file.name, content: file.content })
          }}
          sessions={sessions}
          sessionId={sessionId}
          workspaceTitle={activeSession?.title ?? 'Untitled environment'}
          onSelectSession={(id) => {
            localStorage.setItem('infraagent-session', id)
            window.location.reload()
          }}
          onDeleteSession={(id) => void deleteSession(id)}
          onRenameSession={renameSession}
          operations={operations}
          onOpenOperation={setSelectedOperation}
          onDownloadProject={() => {
            void downloadProject(sessionId).catch((requestError) => {
              setError(requestError instanceof Error ? requestError.message : 'Could not download the Terraform project.')
            })
          }}
        />
        <ChatPanel
          messages={messages}
          message={message}
          busy={busy}
          error={error}
          progress={progress}
          timing={timing}
          onMessageChange={setMessage}
          onSubmit={sendMessage}
          onCancel={() => void cancelAgent()}
          onSelectTemplate={setMessage}
        />
        <Inspector
          checks={checks}
          files={files}
          selectedFile={selectedFile}
          selectedContent={selectedContent}
          busy={busy}
          lastCommand={lastCommand}
          onAction={runAction}
          onOpenFile={() => {
            if (selectedFile) setViewer({ title: selectedFile, content: selectedContent })
          }}
          onShowOutput={() => {
            if (lastCommand) setViewer({ title: 'Terraform command output', content: lastCommand.output || 'No command output.' })
          }}
          changes={checks?.changes ?? []}
          sessionStatus={sessionStatus}
          runCheckov={runCheckov}
          onCancelOperation={() => void cancelOperation()}
          onRunCheckovChange={(value) => {
            setRunCheckov(value)
            localStorage.setItem('infraagent-run-checkov', String(value))
          }}
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
      {viewer && (
        <div className="modal-backdrop" role="presentation" onMouseDown={(event) => {
          if (event.target === event.currentTarget) setViewer(null)
        }}>
          <section className="viewer-modal" role="dialog" aria-modal="true" aria-labelledby="viewer-title">
            <div className="viewer-header">
              <span className="eyebrow">Read-only viewer</span>
              <button type="button" className="viewer-close" onClick={() => setViewer(null)} aria-label="Close viewer">×</button>
            </div>
            <h2 id="viewer-title">{viewer.title}</h2>
            <pre>{viewer.content}</pre>
          </section>
        </div>
      )}
      {selectedOperation && (
        <div className="modal-backdrop" role="presentation" onMouseDown={(event) => {
          if (event.target === event.currentTarget) setSelectedOperation(null)
        }}>
          <section className="operation-modal" role="dialog" aria-modal="true" aria-labelledby="operation-title">
            <div className="viewer-header">
              <span className="eyebrow">Operation details</span>
              <button type="button" className="viewer-close" onClick={() => setSelectedOperation(null)} aria-label="Close operation details">×</button>
            </div>
            <div className="operation-modal-title">
              <div>
                <h2 id="operation-title">{formatOperationKind(selectedOperation.kind)}</h2>
                <span className={`operation-status ${selectedOperation.status}`}>{selectedOperation.status}</span>
              </div>
              <span className={`operation-dot ${selectedOperation.status}`} />
            </div>
            <div className="operation-meta">
              <div><span>Started</span><strong>{formatOperationDate(selectedOperation.started_at)}</strong></div>
              <div><span>Finished</span><strong>{formatOperationDate(selectedOperation.finished_at)}</strong></div>
              <div><span>Result</span><strong>{selectedOperation.ok === null || selectedOperation.ok === undefined ? 'Pending' : selectedOperation.ok ? 'Success' : 'Failed'}</strong></div>
            </div>
            {selectedOperation.summary && <p className="operation-summary">{selectedOperation.summary}</p>}
            <div className="section-label">Command</div>
            <code className="operation-command">{selectedOperation.command || `terraform ${selectedOperation.kind}`}</code>
            <div className="section-label operation-output-label">Terraform output</div>
            <pre className="operation-output">{selectedOperation.output || 'No output recorded.'}</pre>
          </section>
        </div>
      )}
      {showGithub && (
        <div className="modal-backdrop" role="presentation" onMouseDown={(event) => {
          if (event.target === event.currentTarget) setShowGithub(false)
        }}>
          <section className="github-modal" role="dialog" aria-modal="true" aria-labelledby="github-title">
            <div className="viewer-header">
              <span className="eyebrow">Read-only repository access</span>
              <button type="button" className="viewer-close" onClick={() => setShowGithub(false)} aria-label="Close GitHub repositories">×</button>
            </div>
            <h2 id="github-title">{githubAnalysis ? 'Repository analysis' : 'Choose a GitHub repository'}</h2>
            <p className="github-help">InfraAgent reads repository metadata only. It never commits, creates branches, or changes GitHub.</p>
            {githubAnalysis && (
              <div className="github-analysis">
                <div className="github-analysis-heading">
                  <strong>{githubAnalysis.repository}</strong>
                  <span>{githubAnalysis.has_terraform ? 'Terraform detected' : githubAnalysis.deployable_stack ? 'Application stack detected' : 'No deployable stack'}</span>
                </div>
                <div className="github-stats">
                  <span>{githubAnalysis.file_count} files</span>
                  <span>{githubAnalysis.terraform_file_count} Terraform files</span>
                  <span>branch: {githubAnalysis.branch}</span>
                </div>
                <div className="github-technologies">
                  {githubAnalysis.technologies.map((technology) => <span key={technology}>{technology}</span>)}
                </div>
                <p className="github-recommendation">{githubAnalysis.recommendation}</p>
                {githubAnalysis.readme_excerpt && <pre className="github-readme">{githubAnalysis.readme_excerpt}</pre>}
                <div className="github-analysis-actions">
                  {githubAnalysis.has_terraform && (
                    <button type="button" className="primary-button" onClick={() => void importRepository(githubAnalysis)}>
                      Import Terraform and review
                    </button>
                  )}
                  <button type="button" className="secondary-button" onClick={() => {
                    setMessage(githubAnalysis.suggested_prompt)
                    setShowGithub(false)
                    setGithubAnalysis(null)
                  }}>
                    {githubAnalysis.has_terraform ? 'Ask for update proposal' : githubAnalysis.deployable_stack ? 'Propose infrastructure' : 'Ask what to deploy'}
                  </button>
                  <button type="button" className="viewer-close" onClick={() => setGithubAnalysis(null)}>Back</button>
                </div>
              </div>
            )}
            {!githubAnalysis && (githubBusy ? <p className="github-empty">Loading repositories…</p> : (
              <div className="github-repository-list">
                {githubRepositories.map((repository) => (
                  <button type="button" className="github-repository" key={repository.full_name} onClick={() => void analyzeRepository(repository)}>
                    <span>
                      <strong>{repository.full_name}</strong>
                      <small>{repository.private ? 'Private' : 'Public'} · {repository.default_branch}</small>
                    </span>
                    <span>Analyze →</span>
                  </button>
                ))}
                {!githubRepositories.length && <p className="github-empty">No repositories available.</p>}
              </div>
            ))}
          </section>
        </div>
      )}
    </div>
  )
}

function formatOperationKind(kind: string): string {
  return kind.replace(/-/g, ' ').replace(/\b\w/g, (letter) => letter.toUpperCase())
}

function formatOperationDate(value: string | null | undefined): string {
  if (!value) return '—'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString()
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
  const fallback = getWelcomeMessage()
  const stored = localStorage.getItem(`infraagent-messages:${sessionId}`)
  if (!stored) return fallback
  try {
    const messages = JSON.parse(stored) as ChatMessage[]
    return Array.isArray(messages) && messages.length > 0 ? messages : fallback
  } catch {
    return fallback
  }
}

function getWelcomeMessage(): ChatMessage[] {
  return [{
    id: 1,
    role: 'assistant',
    text: 'Describe the infrastructure you need. I will generate Terraform, validate it, and show you the plan before deployment.',
  }]
}

export default App
