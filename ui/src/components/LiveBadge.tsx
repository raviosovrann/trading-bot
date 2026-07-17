/** Red LIVE / grey DRY-RUN indicator — the console's most important signal. */
export function LiveBadge({ live }: { live: boolean }) {
  return (
    <span className={live ? 'badge badge-live' : 'badge badge-dry'}>
      {live ? 'LIVE' : 'DRY-RUN'}
    </span>
  )
}
