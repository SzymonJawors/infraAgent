export type CheckResult = {
  ok: boolean
  missing_binary: boolean
  output: string
  policy_warnings?: string[]
}

export type MessageResponse = {
  reply: string
  files: string[]
  validation: CheckResult
  plan: CheckResult
  scan: CheckResult
  registry_docs: boolean
  plan_summary: PlanSummary
  changes: FileChange[]
}

export type AgentJobStatus = {
  status: 'idle' | 'running' | 'cancelling' | 'completed' | 'cancelled' | 'failed'
  progress: AgentProgress[]
  result?: MessageResponse | null
  error?: string | null
}

export type OperationRecord = {
  id: number
  kind: string
  status: string
  started_at: string
  finished_at?: string | null
  summary: string
}

export type FileChange = {
  name: string
  status: 'added' | 'modified' | 'deleted'
  diff: string
}

export type SessionStatus = {
  status: 'not_started' | 'empty' | 'applied' | 'state_unreadable'
  terraform_state: boolean
  managed_resources: number
}

export type PlanSummary = {
  to_add: number
  to_change: number
  to_destroy: number
  resources: string[]
  cost_warnings: string[]
  estimated_monthly_usd?: number | null
  pricing_status?: 'not_requested' | 'complete' | 'partial' | 'unavailable' | 'no_priced_resources'
  pricing_items?: string[]
}

export type CommandResponse = {
  ok: boolean
  command: string
  output: string
  status?: 'completed' | 'no_managed_resources' | 'failed'
  plan_summary?: PlanSummary | null
}

export type WorkspaceFile = {
  name: string
  content: string
}

export type SessionSummary = {
  id: string
  title: string
  created_at: string
  updated_at: string
}

export type ChatMessage = {
  id: number
  role: 'user' | 'assistant' | 'error'
  text: string
}

export type AgentProgress = {
  stage: string
  message: string
  command?: string
}

export type AgentTiming = {
  label: string
  milliseconds: number
}
