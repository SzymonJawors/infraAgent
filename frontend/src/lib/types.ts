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

export type MessageRequest = {
  message: string
  skip_checkov?: boolean
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
  command: string
  output: string
  ok?: boolean | null
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
  resource_actions?: { address: string; action: 'create' | 'change' | 'destroy' | 'replace' }[]
  cost_warnings: string[]
  estimated_monthly_usd?: number | null
  pricing_status?: 'not_requested' | 'complete' | 'partial' | 'unavailable' | 'no_priced_resources'
  pricing_items?: string[]
}

export type CommandResponse = {
  ok: boolean
  command: string
  output: string
  status?: 'running' | 'completed' | 'no_managed_resources' | 'failed' | 'cancelled'
  operation_id?: number | null
  plan_summary?: PlanSummary | null
  error_analysis?: ErrorAnalysis | null
}

export type ErrorAnalysis = {
  category: string
  title: string
  explanation: string
  recommendation: string
  resource?: string | null
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

export type GithubRepository = {
  full_name: string
  default_branch: string
  private: boolean
  html_url: string
}

export type GithubAnalysis = {
  repository: string
  branch: string
  has_terraform: boolean
  deployable_stack: boolean
  terraform_files: string[]
  terraform_file_count: number
  directories: string[]
  file_count: number
  readme_excerpt: string
  technologies: string[]
  recommendation: string
  suggested_prompt: string
}

export type ChatMessage = {
  id: number
  role: 'user' | 'assistant' | 'error'
  text: string
}

export type ConversationMessage = {
  role: 'user' | 'assistant'
  content: string
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
