import type { CheckResult } from '../lib/types'
import { StatusDot } from './StatusDot'

type CheckRowProps = {
  label: string
  result?: CheckResult
}

export function CheckRow({ label, result }: CheckRowProps) {
  const hasWarnings = Boolean(result?.policy_warnings?.length)
  const state = result ? (result.ok ? (hasWarnings ? 'Warning' : 'Passed') : result.missing_binary ? 'Missing' : 'Failed') : 'Pending'
  return (
    <div className="check-row">
      <StatusDot ok={result?.ok === true && !hasWarnings} warning={!result || result.missing_binary === true || hasWarnings} />
      <span>{label}</span>
      <em className={state.toLowerCase()}>{state}</em>
    </div>
  )
}
