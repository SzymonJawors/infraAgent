import type { CommandResponse, WorkspaceFile, MessageResponse, FileChange, SessionStatus } from '../lib/types'
import { CheckRow } from './CheckRow'
import { StatusDot } from './StatusDot'

type InspectorProps = {
  checks: MessageResponse | null
  files: WorkspaceFile[]
  selectedFile: string | null
  selectedContent: string
  busy: string | null
  lastCommand: CommandResponse | null
  onAction: (path: string, label: string) => void
  changes: FileChange[]
  sessionStatus: SessionStatus | null
}

export function Inspector({
  checks,
  files,
  selectedFile,
  selectedContent,
  busy,
  lastCommand,
  onAction,
  changes,
  sessionStatus,
}: InspectorProps) {
  const canApply = checks?.plan.ok === true && checks.scan.ok === true
  const isApplied = sessionStatus?.status === 'applied'
  const canDestroy =
    lastCommand?.ok === true &&
    lastCommand.status !== 'no_managed_resources' &&
    isDestroyPlan(lastCommand.command) &&
    !isDestroyExecution(lastCommand.command)

  return (
    <aside className="inspector">
      <div className="inspector-header">
        <span className="eyebrow">Run overview</span>
        <span className="more">•••</span>
      </div>
      <div className="readiness-card">
        <div className="readiness-ring">{isApplied || canApply ? '✓' : '—'}</div>
        <div>
          <strong>{isApplied ? 'Infrastructure deployed' : canApply ? 'Ready to deploy' : 'Awaiting configuration'}</strong>
          <span>
            {isApplied
              ? `${sessionStatus?.managed_resources ?? 0} managed Terraform resource${sessionStatus?.managed_resources === 1 ? '' : 's'}`
              : checks
                ? 'Review checks below'
                : files.length
                  ? 'Terraform files loaded'
                  : 'Send a request to begin'}
          </span>
        </div>
      </div>
      <div className="check-list">
        <CheckRow label="Terraform validation" result={checks?.validation} />
        <CheckRow label="Security scan" result={checks?.scan} />
        <CheckRow label="Terraform plan" result={checks?.plan} />
        <div className="check-row">
          <StatusDot ok={Boolean(checks?.registry_docs)} warning />
          <span>Registry documentation</span>
          <em>{checks?.registry_docs ? 'Available' : 'Optional'}</em>
        </div>
        {checks?.plan_summary && <PlanSummaryCard summary={checks.plan_summary} planOutput={checks.plan.output} />}
      </div>
      <div className="action-block">
        <div className="section-label">Actions</div>
        <button className="primary-action" disabled={!canApply || Boolean(busy)} onClick={() => onAction('/apply', 'apply')}>
          {busy === 'apply' ? 'Applying…' : 'Approve & apply'}
          <span>→</span>
        </button>
        <button className="secondary-action" disabled={Boolean(busy) || !files.length} onClick={() => onAction('/plans/destroy', 'destroy-plan')}>
          {busy === 'destroy-plan' ? 'Planning…' : 'Preview destroy'}
        </button>
        <button className="danger-action" disabled={!canDestroy || Boolean(busy)} onClick={() => onAction('/destroy', 'destroy')}>
          {busy === 'destroy' ? 'Destroying…' : 'Approve destroy'}
        </button>
        <p className="action-help">Actions are protected by a file checksum and require a successful security scan.</p>
      </div>
      {changes.length > 0 && <ChangesCard changes={changes} />}
      {lastCommand && <CommandOutput result={lastCommand} />}
      {selectedFile && (
        <div className="code-preview">
          <div className="code-title">
            <span>{selectedFile}</span>
            <span>read-only</span>
          </div>
          <pre>{selectedContent}</pre>
        </div>
      )}
    </aside>
  )
}

function ChangesCard({ changes }: { changes: FileChange[] }) {
  return (
    <div className="changes-card">
      <div className="section-label">Changes before apply</div>
      <strong>{changes.length} Terraform file{changes.length === 1 ? '' : 's'} changed</strong>
      {changes.map((change) => (
        <details className="file-change" key={change.name}>
          <summary>
            <code>{change.name}</code>
            <span className={`change-status ${change.status}`}>{change.status}</span>
          </summary>
          <pre>{change.diff || 'No textual diff available.'}</pre>
        </details>
      ))}
    </div>
  )
}

function PlanSummaryCard({
  summary,
  planOutput,
}: {
  summary: MessageResponse['plan_summary']
  planOutput: string
}) {
  return (
    <div className="plan-summary">
      <div className="section-label">Terraform plan</div>
      <PlanCounts summary={summary} />
      {summary.resources.length > 0 && (
        <div className="resource-list">
          {summary.resources.map((resource) => <code key={resource}>{resource}</code>)}
        </div>
      )}
      <CostPanel summary={summary} />
      <details className="raw-plan">
        <summary>Show raw Terraform output</summary>
        <pre>{planOutput || 'No plan output available.'}</pre>
      </details>
    </div>
  )
}

function CostPanel({ summary }: { summary: MessageResponse['plan_summary'] }) {
  const warnings = summary.cost_warnings
  return (
    <div className={`cost-panel ${warnings.length > 0 ? 'has-warnings' : 'no-warnings'}`}>
      <div className="cost-panel-header">
        <div>
          <span className="section-label">Estimated monthly cost</span>
          <strong>{formatEstimate(summary.estimated_monthly_usd, summary.pricing_status)}</strong>
        </div>
        <span className="cost-badge">AWS</span>
      </div>
      <p className="cost-panel-note">
        Exact pricing depends on region, usage, and AWS billing options. Review these resource-specific signals before applying.
      </p>
      {warnings.length > 0 ? (
        <div className="cost-warning-list">
          {warnings.map((warning) => (
            <div className="cost-warning-item" key={warning}>
              <span>!</span>
              <span>{warning}</span>
            </div>
          ))}
        </div>
      ) : (
        <div className="cost-clear">No cost-intensive resources detected in this plan.</div>
      )}
      {summary.pricing_items && summary.pricing_items.length > 0 && (
        <div className="pricing-items">
          {summary.pricing_items.map((item) => <span key={item}>• {item}</span>)}
        </div>
      )}
      <span className="cost-disclaimer">Heuristic warnings only — not an AWS bill estimate.</span>
    </div>
  )
}

function formatEstimate(value: number | null | undefined, status: MessageResponse['plan_summary']['pricing_status']) {
  if (value !== null && value !== undefined) return `≈ $${value.toFixed(2)} / month`
  if (status === 'no_priced_resources') return 'No priced resources found'
  return 'Estimate unavailable'
}

function CommandOutput({ result }: { result: CommandResponse }) {
  const noManagedResources = result.status === 'no_managed_resources'
  const isDestroyCommand = isDestroyPlan(result.command)
  const isDestroyExecution = isDestroyCommand && /\bterraform\s+destroy\b/.test(result.command)
  const summary = isDestroyCommand ? normalizeDestroySummary(result.plan_summary ?? parseDestroySummary(result.output)) : null
  return (
    <div className={`command-output ${noManagedResources ? 'warning' : result.ok ? 'success' : 'failure'}`}>
      <div>
        <span className="section-label">{isDestroyExecution ? 'Destroy completed' : isDestroyCommand ? 'Destroy preview' : result.command}</span>
        <StatusDot ok={result.ok} />
      </div>
      {noManagedResources ? (
        <p className="command-message">No Terraform-managed resources exist in this state. Apply infrastructure before using destroy.</p>
      ) : isDestroyExecution ? (
        <div className="command-result">
          <p className="command-message">{result.ok ? 'The approved destroy operation completed successfully.' : 'The destroy operation failed.'}</p>
          <pre>{result.output || 'No command output.'}</pre>
        </div>
      ) : isDestroyCommand && summary ? (
        <DestroyPreviewCard summary={summary} output={result.output} />
      ) : (
        <pre>{result.output || 'No command output.'}</pre>
      )}
    </div>
  )
}

function DestroyPreviewCard({ summary, output }: { summary: MessageResponse['plan_summary']; output: string }) {
  return (
    <details className="destroy-preview-card" open>
      <summary>
        <span>
          <strong>{summary.to_destroy} resources to destroy</strong>
          <small>Click to review the complete removal list</small>
        </span>
        <span className="preview-chevron">⌄</span>
      </summary>
      <div className="destroy-preview-body">
        <PlanCounts summary={summary} />
        <div className="destroy-summary">
          <strong>These resources will be permanently removed</strong>
          <span>Review the list before clicking Approve destroy.</span>
        </div>
        {summary.resources.length > 0 ? (
          <div className="destroy-resource-list">
            {summary.resources.map((resource, index) => (
              <div className="destroy-resource" key={resource}>
                <span>{String(index + 1).padStart(2, '0')}</span>
                <code>{resource}</code>
                <b>destroy</b>
              </div>
            ))}
          </div>
        ) : (
          <p className="command-message">Terraform reported resources to destroy, but no resource names were found.</p>
        )}
        <details className="raw-plan">
          <summary>Show raw Terraform details</summary>
          <pre>{output || 'No plan output available.'}</pre>
        </details>
      </div>
    </details>
  )
}

function PlanCounts({ summary }: { summary: MessageResponse['plan_summary'] }) {
  return (
    <div className="plan-counts">
      <span><strong>{summary.to_add}</strong> to add</span>
      <span><strong>{summary.to_change}</strong> to change</span>
      <span><strong>{summary.to_destroy}</strong> to destroy</span>
    </div>
  )
}

function parseDestroySummary(output: string): MessageResponse['plan_summary'] | null {
  const match = output.match(/Plan:\s*(\d+)\s+to add,\s*(\d+)\s+to change,\s*(\d+)\s+to destroy/i)
  if (!match) return null
  const resources = [...output.matchAll(/#\s+([^\s]+)\s+will be destroyed/g)].map(
    ([, resource]) => resource,
  )
  const uniqueResources = [...new Set(resources)].sort()
  return {
    to_add: Number(match[1]),
    to_change: Number(match[2]),
    to_destroy: uniqueResources.length > 0 ? uniqueResources.length : Number(match[3]),
    resources: uniqueResources,
    cost_warnings: [],
  }
}

function isDestroyPlan(command: string): boolean {
  return /(?:^|\s)terraform\s+(?:plan\s+-{1,2}destroy|destroy)(?:\s|$)/.test(command)
}

function isDestroyExecution(command: string): boolean {
  return /\bterraform\s+destroy\b/.test(command)
}

function normalizeDestroySummary(summary: MessageResponse['plan_summary'] | null | undefined) {
  if (!summary) return null
  if (summary.resources.length === 0) return summary
  return {
    ...summary,
    to_add: 0,
    to_change: 0,
    to_destroy: summary.resources.length,
  }
}
