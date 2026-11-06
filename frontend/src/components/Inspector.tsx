import { useState } from 'react'
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
  onOpenFile: () => void
  onShowOutput: () => void
  changes: FileChange[]
  sessionStatus: SessionStatus | null
  runCheckov: boolean
  onRunCheckovChange: (value: boolean) => void
  onCancelOperation: () => void
}

export function Inspector({
  checks,
  files,
  selectedFile,
  selectedContent,
  busy,
  lastCommand,
  onAction,
  onOpenFile,
  onShowOutput,
  changes,
  sessionStatus,
  runCheckov,
  onRunCheckovChange,
  onCancelOperation,
}: InspectorProps) {
  const [showExplanation, setShowExplanation] = useState(false)
  const hasGeneratedFiles = files.length > 0
  const hasReviewOutput = Boolean(
    checks &&
    [checks.validation.output, checks.plan.output, checks.scan.output].some((output) => output.trim()),
  )
  const review = hasGeneratedFiles && hasReviewOutput ? checks : null
  const canApply = review?.plan.ok === true && review.scan.ok === true
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
              : review
                ? 'Review checks below'
                : files.length
                  ? 'Terraform files loaded'
                  : 'Send a request to begin'}
          </span>
        </div>
      </div>
      <div className="check-list">
        <CheckRow label="Terraform validation" result={review?.validation} />
        <CheckRow label="Security scan" result={review?.scan} />
        <CheckRow label="Terraform plan" result={review?.plan} />
        <div className="check-row">
          <StatusDot ok={Boolean(review?.registry_docs)} warning />
          <span>Registry documentation</span>
          <em>{review?.registry_docs ? 'Available' : 'Optional'}</em>
        </div>
        {review?.plan_summary && (
          <PlanSummaryCard
            summary={review.plan_summary}
            planOutput={review.plan.output}
            showExplanation={showExplanation}
            onToggleExplanation={() => setShowExplanation((visible) => !visible)}
          />
        )}
      </div>
      <div className="action-block">
        <div className="section-label">Actions</div>
        {busy && busy !== 'message' && busy !== 'cancelling' && (
          <button type="button" className="cancel-action" onClick={onCancelOperation}>
            Cancel operation
          </button>
        )}
        <button className="secondary-action" aria-busy={busy === 'apply-plan'} disabled={!canApply || Boolean(busy)} onClick={() => onAction('/plans/apply', 'apply-plan')}>
          {busy === 'apply-plan' ? 'Loading plan…' : 'Preview apply'}
        </button>
        <button className="primary-action" aria-busy={busy === 'apply'} disabled={!canApply || Boolean(busy)} onClick={() => onAction('/apply', 'apply')}>
          {busy === 'apply' ? 'Applying…' : 'Approve & apply'}
          <span>→</span>
        </button>
        <button className="secondary-action" aria-busy={busy === 'destroy-plan'} disabled={Boolean(busy) || !files.length} onClick={() => onAction('/plans/destroy', 'destroy-plan')}>
          {busy === 'destroy-plan' ? 'Planning…' : 'Preview destroy'}
        </button>
        <button className="danger-action" aria-busy={busy === 'destroy'} disabled={!canDestroy || Boolean(busy)} onClick={() => onAction('/destroy', 'destroy')}>
          {busy === 'destroy' ? 'Destroying…' : 'Approve destroy'}
        </button>
        <label className="skip-checkov-toggle">
          <input
            type="checkbox"
            checked={runCheckov}
            onChange={(event) => onRunCheckovChange(event.target.checked)}
            disabled={Boolean(busy)}
          />
          <span>
            <strong>Run Checkov security scan</strong>
            <small>{runCheckov ? 'Recommended — validates security policies' : 'Disabled — faster, less secure'}</small>
          </span>
        </label>
        <p className="action-help">
          Actions are protected by a file checksum and require Terraform validation and a plan.
          {!runCheckov && ' Checkov is disabled for the next request.'}
        </p>
      </div>
      {changes.length > 0 && <ChangesCard changes={changes} />}
      {lastCommand?.error_analysis && <ErrorAnalysisCard analysis={lastCommand.error_analysis} onShowOutput={onShowOutput} />}
      {lastCommand && <CommandOutput result={lastCommand} />}
      {selectedFile && (
        <button type="button" className="code-preview" onClick={onOpenFile}>
          <div className="code-title">
            <span>{selectedFile}</span>
            <span>read-only</span>
          </div>
          <pre>{selectedContent}</pre>
        </button>
      )}
    </aside>
  )
}

function ErrorAnalysisCard({
  analysis,
  onShowOutput,
}: {
  analysis: NonNullable<CommandResponse['error_analysis']>
  onShowOutput: () => void
}) {
  return (
    <div className="error-analysis-card">
      <div className="section-label">What happened</div>
      <strong>{analysis.title}</strong>
      <p>{analysis.explanation}</p>
      <div className="error-recommendation">
        <span>Next step</span>
        <p>{analysis.recommendation}</p>
      </div>
      <button type="button" className="log-button" onClick={onShowOutput}>Open full Terraform output</button>
    </div>
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
  showExplanation,
  onToggleExplanation,
}: {
  summary: MessageResponse['plan_summary']
  planOutput: string
  showExplanation: boolean
  onToggleExplanation: () => void
}) {
  return (
    <div className="plan-summary">
      <div className="section-label">Terraform plan</div>
      <PlanCounts summary={summary} />
      <ResourceOverview summary={summary} />
      <button type="button" className="explain-button" onClick={onToggleExplanation}>
        {showExplanation ? 'Hide infrastructure explanation' : 'Explain this infrastructure'}
      </button>
      {showExplanation && <InfrastructureExplanation summary={summary} />}
      <CostPanel summary={summary} />
      <details className="raw-plan">
        <summary>Show raw Terraform output</summary>
        <pre>{planOutput || 'No plan output available.'}</pre>
      </details>
    </div>
  )
}

function ResourceOverview({ summary }: { summary: MessageResponse['plan_summary'] }) {
  const resources = summary.resource_actions?.length
    ? summary.resource_actions
    : summary.resources.map((address) => ({ address, action: 'change' as const }))
  if (resources.length === 0) return null

  return (
    <div className="resource-overview">
      <div className="section-label">Resource overview</div>
      <div className="resource-action-list">
        {resources.map((resource) => (
          <div className={`resource-action ${resource.action}`} key={`${resource.action}-${resource.address}`}>
            <span className="resource-action-icon" aria-hidden="true">
              {resource.action === 'create' ? '+' : resource.action === 'destroy' ? '−' : resource.action === 'replace' ? '↻' : '~'}
            </span>
            <code>{resource.address}</code>
            <span className="resource-action-label">{formatResourceAction(resource.action)}</span>
          </div>
        ))}
      </div>
    </div>
  )
}

function formatResourceAction(action: string): string {
  return action === 'create' ? 'create' : action === 'destroy' ? 'destroy' : action === 'replace' ? 'replace' : 'change'
}

function InfrastructureExplanation({ summary }: { summary: MessageResponse['plan_summary'] }) {
  const actions = summary.resource_actions?.length
    ? summary.resource_actions
    : summary.resources.map((address) => ({ address, action: 'change' as const }))
  const resourceTypes = [...new Set(actions.map(({ address }) => address.split('.')[0]))]
  const securitySignals = [
    summary.cost_warnings.some((warning) => /S3|KMS/i.test(warning)) && 'The plan includes storage encryption or private object storage signals.',
    summary.cost_warnings.some((warning) => /RDS/i.test(warning)) && 'The database has runtime, backup, or multi-AZ cost considerations.',
    summary.cost_warnings.some((warning) => /EC2|NAT|CloudWatch/i.test(warning)) && 'Compute, networking, or logging resources may add usage-based charges.',
  ].filter(Boolean) as string[]

  return (
    <div className="explanation-card">
      <p>
        This plan contains <strong>{actions.length} managed resource{actions.length === 1 ? '' : 's'}</strong>
        {resourceTypes.length > 0 && <> across {resourceTypes.join(', ')}.</>}
        {' '}Terraform will add {summary.to_add}, change {summary.to_change}, and destroy {summary.to_destroy} resource{summary.to_destroy === 1 ? '' : 's'}.
      </p>
      {securitySignals.length > 0 ? (
        <ul>{securitySignals.map((signal) => <li key={signal}>{signal}</li>)}</ul>
      ) : (
        <p>No resource-specific cost signals were detected in this plan. Review the security scan before approval.</p>
      )}
      <span className="explanation-note">This summary is derived from the reviewed Terraform plan and does not modify files.</span>
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
      <span className="cost-disclaimer">
        {summary.pricing_status === 'complete'
          ? 'Estimate from AWS Pricing API using 730 hours/month; storage, traffic and taxes may add cost.'
          : summary.pricing_status === 'partial'
            ? 'Partial AWS Pricing API estimate; some resources or usage-based charges were unavailable.'
            : summary.pricing_status === 'no_priced_resources'
              ? 'No direct AWS Pricing API estimate for these resource types; storage, requests, and taxes may still cost money.'
              : 'No AWS Pricing API estimate available; warnings are based on the Terraform plan.'}
      </span>
    </div>
  )
}

function formatEstimate(value: number | null | undefined, status: MessageResponse['plan_summary']['pricing_status']) {
  if (value !== null && value !== undefined) return `≈ $${value.toFixed(2)} / month`
  if (status === 'no_priced_resources') return 'No priced resources found'
  return 'Estimate unavailable'
}

function CommandOutput({ result }: { result: CommandResponse }) {
  const running = result.status === 'running'
  const cancelled = result.status === 'cancelled'
  const noManagedResources = result.status === 'no_managed_resources'
  const isDestroyCommand = isDestroyPlan(result.command)
  const isDestroyExecution = isDestroyCommand && /\bterraform\s+destroy\b/.test(result.command)
  const summary = isDestroyCommand ? normalizeDestroySummary(result.plan_summary ?? parseDestroySummary(result.output)) : null
  return (
    <div className={`command-output ${running ? 'running' : cancelled ? 'warning' : noManagedResources ? 'warning' : result.ok ? 'success' : 'failure'}`}>
      <div>
        <span className="section-label">{running ? 'Terraform operation running' : cancelled ? 'Operation cancelled' : isDestroyExecution ? 'Destroy completed' : isDestroyCommand ? 'Destroy preview' : result.command}</span>
        <StatusDot ok={result.ok} />
      </div>
      {running ? (
        <div className="command-result">
          <p className="command-message">Terraform is still running. Live output will appear below.</p>
          <pre>{result.output || 'Waiting for Terraform output.'}</pre>
        </div>
      ) : cancelled ? (
        <p className="command-message">The Terraform operation was cancelled. No further changes are being applied.</p>
      ) : noManagedResources ? (
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
