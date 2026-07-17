/** Tiny SVG sparkline of PnL samples observed while the page is open. */
export function PnlSparkline({
  values,
  width = 180,
  height = 40,
}: {
  values: number[]
  width?: number
  height?: number
}) {
  if (values.length < 2) {
    return <span className="muted">collecting…</span>
  }
  const min = Math.min(...values)
  const max = Math.max(...values)
  const range = max - min || 1
  const pad = 2
  const points = values
    .map((v, i) => {
      const x = pad + (i / (values.length - 1)) * (width - 2 * pad)
      const y = height - pad - ((v - min) / range) * (height - 2 * pad)
      return `${x.toFixed(1)},${y.toFixed(1)}`
    })
    .join(' ')
  const up = values[values.length - 1] >= values[0]
  return (
    <svg width={width} height={height} role="img" aria-label="PnL trend" className="sparkline">
      <polyline points={points} fill="none" stroke={up ? '#22c55e' : '#ef4444'} strokeWidth="1.5" />
    </svg>
  )
}
