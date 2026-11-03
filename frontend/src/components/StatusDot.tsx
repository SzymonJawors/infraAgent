type StatusDotProps = {
  ok: boolean
  warning?: boolean
}

export function StatusDot({ ok, warning = false }: StatusDotProps) {
  return <span className={`status-dot ${ok ? 'is-ok' : warning ? 'is-warning' : 'is-idle'}`} />
}
