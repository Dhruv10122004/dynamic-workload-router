import { useEffect, useRef, useState } from 'react';
import { fmtNum } from '../api.js';
import Icon from '../components/Icons.jsx';
import { CONTROLLER, QUEUE, diffLive } from './liveDiff.js';

// Live network map (DECISIONS D-030): controller in the middle, one node per laptop around
// it, the overflow queue on top. Dots fly along the links for every real move between two
// polls (see liveDiff.js). Worker order and accent come from the API order (tier_rank).

const W = 1000;
const C = { x: 500, y: 200 };          // controller
const Q = { x: 500, y: 52 };           // queue
const NODE_W = 260;
const NODE_H = 132;
const FLIGHT_MS = 2000;
const QUEUE_DOTS = 24;

function layout(workers) {
  const pos = {};
  workers.forEach((w, i) => {
    const row = Math.floor(i / 2);
    pos[w.worker_id] = { x: i % 2 === 0 ? 165 : 835, y: 200 + row * 160 };
  });
  return { pos, height: 290 + Math.max(0, Math.ceil(workers.length / 2) - 1) * 160 };
}

function slotXY(p, k) {
  return { x: p.x - 52 + (k % 4) * 42 + 17, y: p.y + 20 + Math.floor(k / 4) * 28 };
}

function endpoint(id, live, pos, jobId) {
  if (id === CONTROLLER) return C;
  if (id === QUEUE) return { x: Q.x, y: Q.y + 18 };
  const p = pos[id];
  if (!p) return C;
  const w = live.workers.find((x) => x.worker_id === id);
  const k = w ? w.slots.findIndex((s) => s.job && s.job.job_id === jobId) : -1;
  return k >= 0 ? slotXY(p, k) : { x: p.x, y: p.y + 40 };
}

function Flight({ f }) {
  const [go, setGo] = useState(false);
  useEffect(() => {
    const id = requestAnimationFrame(() => requestAnimationFrame(() => setGo(true)));
    return () => cancelAnimationFrame(id);
  }, []);
  if (f.pop) {
    return <circle className={`map-pop k-${f.kind}`} cx={f.to.x} cy={f.to.y} r="16" />;
  }
  const at = go ? f.to : f.from;
  return (
    <g className={`map-dot k-${f.kind} ${go ? 'go' : ''}`} style={{ transform: `translate(${at.x}px, ${at.y}px)` }}>
      <circle r="11" />
      {f.n != null && <text y="4">{f.n}</text>}
    </g>
  );
}

function Ring({ x, y, pct }) {
  const r = 22;
  const c = 2 * Math.PI * r;
  const p = pct == null ? 0 : Math.min(100, Math.max(0, pct)) / 100;
  return (
    <g className="map-ring">
      <circle cx={x} cy={y} r={r} className="ring-bg" />
      <circle cx={x} cy={y} r={r} className="ring-fg" strokeDasharray={`${c * p} ${c}`}
              transform={`rotate(-90 ${x} ${y})`} />
      <text x={x} y={y + 4} className="ring-text">{pct == null ? '–' : `${fmtNum(pct, 0)}%`}</text>
      <text x={x} y={y + 36} className="ring-cap">CPU</text>
    </g>
  );
}

function WorkerNode({ w, p, accent, flow, onFocus, onWhy }) {
  const tip = `${w.worker_id} (${w.tier}) on ${w.hostname} · ${w.os}\nCPU ${fmtNum(w.cpu_pct, 0)}% · RAM ${fmtNum(w.mem_pct, 0)}%`
    + (w.foreign_cpu_mean != null ? ` · other programs ${fmtNum(w.foreign_cpu_mean, 0)}%` : '')
    + `\n${w.slots.map((s) => `slot ${s.slot}: core ${s.cpus.join('+')}`).join(' · ')}`;
  const cls = `map-node acc-${accent} ${w.fresh ? '' : 'stale'} ${w.host_busy ? 'busy' : ''} ${flow ? 'flow' : ''}`;
  return (
    <g className={cls} onClick={() => onFocus(w.worker_id)}>
      <title>{tip}</title>
      <rect x={p.x - NODE_W / 2} y={p.y - NODE_H / 2} width={NODE_W} height={NODE_H} rx="14" className="node-box" />
      <rect x={p.x - NODE_W / 2} y={p.y - NODE_H / 2} width={NODE_W} height="6" rx="3" className="node-accent" />
      <text x={p.x - NODE_W / 2 + 16} y={p.y - 36} className="node-title">{w.worker_id}</text>
      <text x={p.x - NODE_W / 2 + 16} y={p.y - 18} className="node-sub">{w.tier} · {w.hostname}</text>
      {!w.fresh && <text x={p.x + NODE_W / 2 - 14} y={p.y - 36} className="node-flag bad">OFFLINE</text>}
      {w.fresh && w.host_busy && <text x={p.x + NODE_W / 2 - 14} y={p.y - 36} className="node-flag warn">BUSY</text>}
      <Ring x={p.x - NODE_W / 2 + 42} y={p.y + 22} pct={w.fresh ? w.cpu_pct : null} />
      {w.slots.map((s, k) => {
        const q = slotXY(p, k);
        const job = s.job;
        const state = !job ? 'free' : job.offline ? 'offline' : 'run';
        return (
          <g key={s.slot} className={`slot-pill ${state}`}
             onClick={(e) => { if (job) { e.stopPropagation(); onWhy(job.job_id); } }}>
            <title>{`slot ${s.slot} (core ${s.cpus.join('+')})${job ? `: #${job.n} ${job.label}${job.pid ? `, PID ${job.pid}` : ''}` : ': free'}`}</title>
            <rect x={q.x - 17} y={q.y - 11} width="34" height="22" rx="6" />
            <text x={q.x} y={q.y + 4}>{job ? (job.offline ? '?' : job.n) : ''}</text>
          </g>
        );
      })}
      {w.arriving.length > 0 &&
        <text x={p.x + 18} y={p.y + NODE_H / 2 - 10} className="node-sub">+{w.arriving.length} arriving</text>}
    </g>
  );
}

export default function NetworkMap({ live, labels, legend, onWhy, onFocusWorker }) {
  const [flights, setFlights] = useState([]);
  const prev = useRef(null);
  const seq = useRef(0);
  const timers = useRef([]);
  const { pos, height } = layout(live.workers);

  useEffect(() => {
    const moves = diffLive(prev.current, live);
    prev.current = live;
    if (!moves.length) return undefined;
    const { pos } = layout(live.workers);
    const born = moves.map((m) => ({
      key: `${m.job_id}-${m.kind}-${seq.current++}`,
      kind: m.kind, n: m.n, pop: m.from === m.to,
      from: endpoint(m.from, live, pos, m.job_id), to: endpoint(m.to, live, pos, m.job_id),
      via: [m.from, m.to],
    }));
    const keys = new Set(born.map((b) => b.key));
    // not cleared on the next poll (that would leave dots behind); only on unmount
    timers.current.push(setTimeout(() => setFlights((f) => [...f, ...born]), 0));
    timers.current.push(setTimeout(() => setFlights((f) => f.filter((x) => !keys.has(x.key))), FLIGHT_MS));
    if (timers.current.length > 400) timers.current = timers.current.slice(-200);
    return undefined;
  }, [live]);

  useEffect(() => () => timers.current.forEach(clearTimeout), []);

  const busyLinks = new Set(flights.flatMap((f) => f.via));
  const queue = live.queue;
  return (
    <div className="map-wrap">
      <svg className="map-svg" viewBox={`0 0 ${W} ${height}`} role="img" aria-label="live network map">
        {/* links */}
        <line x1={C.x} y1={C.y} x2={Q.x} y2={Q.y + 26} className={`map-link to-queue ${busyLinks.has(QUEUE) ? 'flow' : ''}`} />
        {live.workers.map((w, i) => {
          const p = pos[w.worker_id];
          const side = p.x < C.x ? 1 : -1;
          return (
            <g key={w.worker_id}>
              <line x1={C.x} y1={C.y} x2={p.x + side * NODE_W / 2} y2={p.y}
                    className={`map-link has-acc acc-${i % 4}${w.fresh ? '' : 'down'} ${busyLinks.has(w.worker_id) ? 'flow' : ''}`} />
              <line x1={Q.x + side * -150} y1={Q.y} x2={p.x} y2={p.y - NODE_H / 2} className="map-link pick-path" />
            </g>
          );
        })}

        {/* queue */}
        <g className={`map-queue ${queue.length ? 'has' : ''}`}>
          <title>{labels.queue_note}</title>
          <rect x={Q.x - 150} y={Q.y - 34} width="300" height="68" rx="14" className="node-box" />
          <text x={Q.x - 136} y={Q.y - 14} className="node-title">{labels.queue_title}</text>
          <text x={Q.x + 136} y={Q.y - 14} className="queue-count">{queue.length} waiting</text>
          {queue.slice(0, QUEUE_DOTS).map((q, i) => (
            <g key={q.job_id} className="queue-dot" onClick={() => onWhy(q.job_id)}>
              <title>{`#${q.n} ${q.label}: waiting ${fmtNum(q.waiting_s, 0)} s`}</title>
              <circle cx={Q.x - 136 + i * 11.4 + 5} cy={Q.y + 14} r="4.5" />
            </g>
          ))}
          {queue.length === 0 && <text x={Q.x - 136} y={Q.y + 18} className="node-sub">{labels.queue_empty}</text>}
          {queue.length > QUEUE_DOTS && <text x={Q.x + 136} y={Q.y + 18} className="queue-count">+{queue.length - QUEUE_DOTS}</text>}
        </g>

        {/* controller */}
        <g className="map-ctl">
          <circle cx={C.x} cy={C.y} r="38" />
          <g transform={`translate(${C.x - 14} ${C.y - 22})`}><Icon name="server" size={28} /></g>
          <text x={C.x} y={C.y + 22} className="ctl-text">controller</text>
        </g>

        {live.workers.map((w, i) => (
          <WorkerNode key={w.worker_id} w={w} p={pos[w.worker_id]} accent={i % 4}
                      flow={busyLinks.has(w.worker_id)} onFocus={onFocusWorker} onWhy={onWhy} />
        ))}

        {flights.map((f) => <Flight key={f.key} f={f} />)}
      </svg>
      <div className="map-legend">
        {legend.map((l) => <span key={l.kind}><i className={`lg k-${l.kind}`} />{l.text}</span>)}
      </div>
    </div>
  );
}
