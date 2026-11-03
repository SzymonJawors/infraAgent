import type { AgentProgress, MessageResponse } from './types'

const API_BASE = import.meta.env.VITE_API_URL ?? 'http://127.0.0.1:8000'
const WS_BASE = API_BASE.replace(/^http/, 'ws')
const activeSockets = new Map<string, WebSocket>()
export async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  })
  if (response.status === 204) return undefined as T
  const payload = (await response.json()) as T | { detail?: string }
  if (!response.ok) {
    const detail =
      typeof payload === 'object' && payload !== null && 'detail' in payload && payload.detail
        ? payload.detail
        : 'Request failed'
    throw new Error(detail)
  }
  return payload as T
}

export async function downloadProject(sessionId: string): Promise<void> {
  const response = await fetch(`${API_BASE}/sessions/${sessionId}/export`)
  if (!response.ok) {
    let detail = 'Could not download the Terraform project.'
    try {
      const payload = (await response.json()) as { detail?: string }
      if (payload.detail) detail = payload.detail
    } catch {
      // Keep the user-facing fallback when the server does not return JSON.
    }
    throw new Error(detail)
  }

  const blob = await response.blob()
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = `infraagent-${sessionId}.zip`
  document.body.appendChild(link)
  link.click()
  link.remove()
  URL.revokeObjectURL(url)
}

export function getSessionId(): string {
  const stored = localStorage.getItem('infraagent-session')
  if (stored) return stored
  const id = `session-${crypto.randomUUID().slice(0, 8)}`
  localStorage.setItem('infraagent-session', id)
  return id
}

export function createSessionId(): string {
  return `session-${crypto.randomUUID().slice(0, 8)}`
}

export async function streamMessage(
  path: string,
  body: object,
  onProgress: (progress: AgentProgress) => void,
): Promise<MessageResponse> {
  const response = await fetch(`${API_BASE}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
    body: JSON.stringify(body),
  })
  if (!response.ok) {
    const payload = (await response.json()) as { detail?: string }
    throw new Error(payload.detail || 'Request failed')
  }

  if (!response.body) throw new Error('Streaming is not supported by this browser')

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  let result: MessageResponse | null = null

  while (true) {
    const { done, value } = await reader.read()
    buffer += decoder.decode(value, { stream: !done })
    const events = buffer.split('\n\n')
    buffer = events.pop() ?? ''
    for (const rawEvent of events) {
      const event = parseEvent(rawEvent)
      if (!event) continue
      if (event.type === 'status') onProgress(event.data as AgentProgress)
      if (event.type === 'result') result = event.data as MessageResponse
      if (event.type === 'error') throw new Error(String((event.data as { message: string }).message))
    }
    if (done) break
  }
  if (!result) throw new Error('The agent stream ended without a result')
  return result
}

export async function streamMessageWebSocket(
  sessionId: string,
  body: object,
  onProgress: (progress: AgentProgress) => void,
): Promise<MessageResponse> {
  const socket = new WebSocket(`${WS_BASE}/sessions/${sessionId}/messages/ws`)
  activeSockets.set(sessionId, socket)
  return new Promise((resolve, reject) => {
    socket.onopen = () => socket.send(JSON.stringify(body))
    socket.onmessage = (event) => {
      const payload = JSON.parse(event.data) as { type: string; data: unknown }
      if (payload.type === 'status') onProgress(payload.data as AgentProgress)
      if (payload.type === 'result') {
        activeSockets.delete(sessionId)
        socket.close()
        resolve(payload.data as MessageResponse)
      }
      if (payload.type === 'error') {
        activeSockets.delete(sessionId)
        socket.close()
        reject(new Error(String((payload.data as { message: string }).message)))
      }
    }
    socket.onerror = () => {
      activeSockets.delete(sessionId)
      reject(new Error('WebSocket connection failed'))
    }
    socket.onclose = (event) => {
      activeSockets.delete(sessionId)
      if (!event.wasClean) reject(new Error('WebSocket connection closed unexpectedly'))
    }
  })
}

export function cancelMessageWebSocket(sessionId: string): void {
  const socket = activeSockets.get(sessionId)
  if (socket?.readyState === WebSocket.OPEN) socket.send('cancel')
}

function parseEvent(rawEvent: string): { type: string; data: unknown } | null {
  const type = rawEvent.match(/^event: (.+)$/m)?.[1]
  const data = rawEvent.match(/^data: (.+)$/m)?.[1]
  if (!type || !data) return null
  return { type, data: JSON.parse(data) }
}
