// Tiny line chart of values actually polled (no smoothing, no invented points).
export default function Sparkline({ points: raw, width = 160, height = 32, label }) {
  if (!raw || raw.length < 2) return <div className="spark-empty">{label}: collecting…</div>;
  const points = [...raw].sort((a, b) => a.t - b.t);
  const t0 = points[0].t;
  const span = Math.max(1, points[points.length - 1].t - t0);
  const peak = Math.max(...points.map((p) => p.v));
  const max = Math.max(1, peak);          // scale only; the label shows the real peak
  const xy = points.map((p) => [((p.t - t0) / span) * (width - 2) + 1, height - 2 - (p.v / max) * (height - 6)]);
  const line = xy.map(([x, y], i) => `${i ? 'L' : 'M'}${x.toFixed(1)},${y.toFixed(1)}`).join(' ');
  const last = points[points.length - 1].v;
  return (
    <div className="spark" title={`${label}: now ${last}, highest ${peak}`}>
      <svg width="100%" height={height} viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none" aria-hidden="true">
        <path d={`${line} L${xy[xy.length - 1][0].toFixed(1)},${height - 1} L${xy[0][0].toFixed(1)},${height - 1} Z`}
              className="spark-area" />
        <path d={line} className="spark-line" />
      </svg>
      <span className="spark-label">{label} · now <b>{last}</b> · peak {peak}</span>
    </div>
  );
}
