import { useState } from 'react'
import type { OperationRecord, SessionSummary, WorkspaceFile } from '../lib/types'

type SidebarProps = {
  files: WorkspaceFile[]
  selectedFile: string | null
  onSelectFile: (name: string) => void
  onOpenFile: (name: string) => void
  sessions: SessionSummary[]
  sessionId: string
  workspaceTitle: string
  onSelectSession: (id: string) => void
  onDeleteSession: (id: string) => void
  onRenameSession: (id: string, title: string) => Promise<void>
  operations: OperationRecord[]
  onOpenOperation: (operation: OperationRecord) => void
  onDownloadProject: () => void
}

export function Sidebar({
  files,
  selectedFile,
  onSelectFile,
  onOpenFile,
  sessions,
  sessionId,
  workspaceTitle,
  onSelectSession,
  onDeleteSession,
  onRenameSession,
  operations,
  onOpenOperation,
  onDownloadProject,
}: SidebarProps) {
  const [editingId, setEditingId] = useState<string | null>(null)
  const [editingTitle, setEditingTitle] = useState('')

  function startEditing(session: SessionSummary) {
    setEditingId(session.id)
    setEditingTitle(session.title)
  }

  async function saveTitle(id: string) {
    const title = editingTitle.trim()
    if (!title) return
    await onRenameSession(id, title)
    setEditingId(null)
  }

  return (
    <aside className="sidebar">
      <div className="sidebar-section">
        <div className="section-label">Workspace</div>
        <div className="workspace-card">
          <div className="workspace-icon">⌁</div>
          <div>
            <strong>{workspaceTitle}</strong>
            <span>{workspaceTitle.includes('/') ? 'GitHub repository' : 'Local session'}</span>
          </div>
          <span className="live-dot" />
        </div>
      </div>
      <div className="sidebar-section files-section">
        <div className="section-heading">
          <span className="section-label">Sessions</span>
          <span className="count-badge">{sessions.length}</span>
        </div>
        <div className="file-list session-list">
          {sessions.map((session) => (
            <div className={`session-item ${session.id === sessionId ? 'active' : ''}`} key={session.id}>
              {editingId === session.id ? (
                <input
                  className="session-title-input"
                  value={editingTitle}
                  autoFocus
                  maxLength={80}
                  onChange={(event) => setEditingTitle(event.target.value)}
                  onBlur={() => void saveTitle(session.id)}
                  onKeyDown={(event) => {
                    if (event.key === 'Enter') void saveTitle(session.id)
                    if (event.key === 'Escape') setEditingId(null)
                  }}
                  aria-label="Session name"
                />
              ) : (
                <button className="session-open" onClick={() => onSelectSession(session.id)}>
                  <span className="file-icon">AI</span>
                  <span className="session-title">{session.title}</span>
                </button>
              )}
              <button
                className="session-rename"
                type="button"
                aria-label={`Rename ${session.title}`}
                onClick={() => startEditing(session)}
              >
                ✎
              </button>
              <button
                className="session-delete"
                type="button"
                aria-label={`Delete ${session.title}`}
                onClick={() => onDeleteSession(session.id)}
              >
                ×
              </button>
            </div>
          ))}
        </div>
      </div>
      <div className="sidebar-section files-section">
        <div className="section-heading">
          <span className="section-label">Operation history</span>
          <span className="count-badge">{operations.length}</span>
        </div>
        <div className="operation-list">
          {operations.slice(0, 6).map((operation) => (
            <button
              type="button"
              className="operation-item"
              key={operation.id}
              onClick={() => onOpenOperation(operation)}
            >
              <span className={`operation-dot ${operation.status}`} />
              <span>
                <strong>{operation.kind}</strong>
                <small>{operation.status}</small>
              </span>
              <span className="operation-open">›</span>
            </button>
          ))}
          {!operations.length && <div className="empty-files">No operations yet.</div>}
        </div>
      </div>
      <div className="sidebar-section files-section">
        <div className="section-heading">
          <span className="section-label">Terraform files</span>
          <span className="count-badge">{files.length}</span>
        </div>
        {files.length ? (
          <div className="file-list">
            {files.map((file) => (
              <button
                className={`file-item ${selectedFile === file.name ? 'active' : ''}`}
                key={file.name}
                onClick={() => {
                  onSelectFile(file.name)
                  onOpenFile(file.name)
                }}
              >
                <span className="file-icon">TF</span>
                <span>{file.name}</span>
              </button>
            ))}
          </div>
        ) : (
          <div className="empty-files">Generated files will appear here.</div>
        )}
      </div>
      <div className="sidebar-footer">
        <button
          type="button"
          className="download-project"
          disabled={!files.length}
          onClick={onDownloadProject}
        >
          <span>↓</span>
          Download Terraform project
        </button>
        <div className="security-note">
          <span>✦</span>
          <p>
            <strong>Safe by default</strong>
            <br />
            No changes run without approval.
          </p>
        </div>
      </div>
    </aside>
  )
}
