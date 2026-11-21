import { useEffect, useRef, useState } from 'react';
import { api, fmtNum, usePoll } from '../api.js';
import TaskForm from '../components/TaskForm.jsx';
import { ErrorLine, Stat } from './common.jsx';

// Classroom playground (DECISIONS D-027). Every label, explanation and story line comes from
// /api/v1/playground/*; this file only lays them out and animates arrivals.

const FLASH_MS = 3500;

function useLive() {
  const [state, setState] = useState({ data: null, feed: [], error: null });
  const cursor = useRef(0);
  const busy = useRef(false);
  useEffect(() => {
    let alive = true;
    const tick = async () => {
      if (busy.current) return;
      busy.current = true;
      try {
        const data = await api(`/playground/live?after=${cursor.current}`);
        cursor.current = data.cursor;
        if (alive) setState((s) => ({ data, error: null, feed: [...s.feed, ...data.feed].slice(-80) }));
      } catch (e) {
        if (alive) setState((s) => ({ ...s, error: e.message }));
      } finally {
        busy.current = false;
      }
    };
    tick();
    const id = setInterval(tick, 1000);
    return () => { alive = false; clearInterval(id); };
  }, []);
  return state;
}

function ActivityBar({ catalog, activity, onError }) {
  const [custom, setCustom] = useState({ load: 1.5, mix: 'mixed', policy: 'LocalFirst', duration_s: 60 });
  const [showCustom, setShowCustom] = useState(false);
  const running = activity && activity.running;
  const start = async (body) => {
    try { await api('/playground/activity', { method: 'POST', body }); onError(null); } catch (e) { onError(e.message); }
  };
  const stop = async () => {
    try { await api('/playground/activity/stop', { method: 'POST' }); } catch (e) { onError(e.message); }
  };
  const lim = catalog.custom_limits;
  return (
    <div className="panel">
      <div className="pg-row">
        <h3>Activities</h3>
        {running && <button className="stop" onClick={stop}>■ stop “{activity.title}”</button>}
        <button className="small" onClick={() => setShowCustom(!showCustom)}>{showCustom ? 'hide custom' : 'custom…'}</button>
      </div>
      <div className="scenarios">
        {catalog.scenarios.map((s) => (
          <button key={s.id} className={`scenario ${activity && activity.title === s.title && running ? 'on' : ''}`}
                  disabled={running} onClick={() => start({ scenario: s.id })} title={s.needs || ''}>
            <b>{s.title}</b>
            <span>{s.watch}</span>
            {s.needs && <em>needs: {s.needs}</em>}
          </button>
        ))}
      </div>
      {showCustom && (
        <div className="custom">
          <label>load × capacity: <b>{custom.load}</b>
            <input type="range" min={lim.load[0]} max={lim.load[1]} step="0.25" value={custom.load}
                   onChange={(e) => setCustom({ ...custom, load: parseFloat(e.target.value) })} />
          </label>
          <label>mix
            <select value={custom.mix} onChange={(e) => setCustom({ ...custom, mix: e.target.value })}>
              {catalog.mixes.map((m) => <option key={m}>{m}</option>)}
            </select>
          </label>
          <label>policy
            <select value={custom.policy} onChange={(e) => setCustom({ ...custom, policy: e.target.value })}>
              {catalog.policies.map((p) => <option key={p}>{p}</option>)}
            </select>
          </label>
          <label>duration: <b>{custom.duration_s} s</b>
            <input type="range" min={lim.duration_s[0]} max={lim.duration_s[1]} step="10" value={custom.duration_s}
                   onChange={(e) => setCustom({ ...custom, duration_s: parseInt(e.target.value, 10) })} />
          </label>
          <button disabled={running} onClick={() => start({ custom })}>start custom</button>
        </div>
      )}
      {activity && (
        <div className="activity">
          <div><b>{activity.title}</b> · {activity.policy_text}{running ? '' : ' · finished'}</div>
          <div className="dim">{activity.watch}</div>
          {activity.total > 0 && (
            <div className="bar"><div style={{ width: `${(100 * activity.submitted) / activity.total}%` }} />
              <span>{activity.submitted}/{activity.total} jobs submitted · {fmtNum(activity.elapsed_s, 0)} s
                {activity.duration_s ? ` of ${activity.duration_s} s` : ''}</span></div>
          )}
        </div>
      )}
      {activity && activity.cue && <div className="cue">{activity.cue}</div>}
    </div>
  );
}

function JobTile({ t, flash, onWhy }) {
  const progress = t.expected_s ? Math.min(100, (100 * t.elapsed_s) / t.expected_s) : null;
  const cancel = async (e) => {
    e.stopPropagation();
    try { await api(`/jobs/${t.job_id}/cancel`, { method: 'POST' }); } catch { /* shown by the story feed */ }
  };
  return (
    <div className={`tile ${t.offline ? 'offline' : ''} ${flash ? 'flash' : ''}`} onClick={() => onWhy(t.job_id)}>
      <div className="tile-head"><b>#{t.n}</b> {t.label}
        {!t.offline && !t.cancel_requested && t.state === 'RUNNING' &&
          <button className="small cancel" onClick={cancel}>cancel</button>}
      </div>
      {t.offline ? <div className="offline-text">{t.offline_text}</div> : (
        <>
          <div className="tile-meta">
            {t.pid ? <>PID <b>{t.pid}</b></> : <span className="dim">{t.phase}</span>}
            {t.cores && <> · core {t.cores.join('+')}</>}
            {t.rss_mb != null && <> · {fmtNum(t.rss_mb, 0)} MB</>}
          </div>
          {t.cpu_pct != null && (
            <div className="meter" title="CPU of this job (100% = one core)">
              <div style={{ width: `${Math.min(100, t.cpu_pct)}%` }} /><span>CPU {fmtNum(t.cpu_pct, 0)}%</span>
            </div>
          )}
          {progress != null && <div className="meter thin"><div style={{ width: `${progress}%` }} /></div>}
          <div className="tile-meta dim">{fmtNum(t.elapsed_s, 1)} s{t.expected_s ? ` / ≈${fmtNum(t.expected_s, 1)} s` : ''}
            {t.cancel_requested && ' · cancelling…'}</div>
        </>
      )}
      {flash && t.from_queue_after_s != null && <div className="picked">picked from the queue after {t.from_queue_after_s} s</div>}
    </div>
  );
}

function WorkerCard({ w, flashes, labels, onWhy }) {
  return (
    <div className={`pg-worker ${w.fresh ? '' : 'is-stale'} ${w.host_busy ? 'is-busy' : ''}`}>
      <div className="card-head">
        <span className="wid">{w.worker_id}</span><span className="tier">{w.tier}</span>
        {w.fresh ? <span className="badge good">online</span> : <span className="badge bad">STALE</span>}
        {w.host_busy && <span className="badge warn">HOST BUSY</span>}
        {w.draining && <span className="badge warn">DRAINING</span>}
      </div>
      <div className="sub">{w.hostname} · {w.os} · CPU {fmtNum(w.cpu_pct, 0)}% · RAM {fmtNum(w.mem_pct, 0)}%
        {!w.fresh && w.heartbeat_age_s != null && <span className="bad-text"> · no heartbeat for {fmtNum(w.heartbeat_age_s, 0)} s</span>}
      </div>
      {w.slots.map((s) => (
        <div key={s.slot} className="lane">
          <div className="lane-label">slot {s.slot}<br /><span className="dim">core {s.cpus.join('+')}</span></div>
          {s.job ? <JobTile t={{ ...s.job, offline_text: labels.offline_tile }} flash={flashes[s.job.job_id]} onWhy={onWhy} />
            : <div className="idle">idle</div>}
        </div>
      ))}
      {w.arriving.length > 0 && (
        <div className="arriving">{w.arriving.map((t) => (
          <JobTile key={t.job_id} t={{ ...t, offline_text: labels.offline_tile }} flash={flashes[t.job_id]} onWhy={onWhy} />
        ))}</div>
      )}
    </div>
  );
}

function QueueColumn({ queue, labels, board, onWhy }) {
  return (
    <div className="pg-queue">
      <div className="card-head"><span className="wid">{labels.queue_title}</span>
        <span className="badge">{queue.length} waiting</span></div>
      <div className="sub">{labels.queue_note}</div>
      {board && <div className="demand">{labels.demand_label}: <b>{fmtNum(board.overflow_demand_s, 1)} s</b> · {board.overflow_jobs} jobs had to wait</div>}
      {queue.length === 0 && <div className="idle">{labels.queue_empty}</div>}
      <div className="chips">
        {queue.map((q) => (
          <div key={q.job_id} className="chip" onClick={() => onWhy(q.job_id)}>
            <b>#{q.n}</b> {q.label} <span className="dim">waiting {fmtNum(q.waiting_s, 0)} s</span>
          </div>
        ))}
      </div>
    </div>
  );
}

function WhyCard({ jobId, onClose }) {
  const { data } = usePoll(`/playground/explain/${jobId}`, 1000);
  if (!data) return null;
  return (
    <div className={`why ${data.clicked ? 'clicked' : ''}`}>
      <button className="small close" onClick={() => onClose(jobId)}>×</button>
      <div className="why-head">{data.headline}</div>
      <ul>{data.lines.map((l) => <li key={l}>{l}</li>)}</ul>
      <div className="timeline">{data.timeline.map((e) => (
        <span key={`${e.t}${e.state}`} className={`state ${e.state}`}>{e.state} +{e.t}s</span>
      ))}</div>
    </div>
  );
}

function TaskPalette({ tasks, onLaunched, onError }) {
  const [custom, setCustom] = useState(false);
  const launch = async (body) => {
    const r = await api('/playground/task', { method: 'POST', body });
    onLaunched(r.jobs.map((j) => j.job_id));
  };
  const click = async (id) => {
    try { await launch({ task_id: id }); onError(null); } catch (e) { onError(e.message); }
  };
  return (
    <div className="panel">
      <div className="pg-row"><h3>Launch a task yourself</h3>
        <button className="small" onClick={() => setCustom(!custom)}>{custom ? 'hide builder' : 'custom task…'}</button></div>
      <div className="palette">
        {tasks.map((t) => (
          <button key={t.id} onClick={() => click(t.id)}><b>{t.label}</b><span>{t.hint}</span></button>
        ))}
      </div>
      {custom && (
        <div className="builder">
          <TaskForm maxCount={10} showTimeout={false} submitLabel="launch"
                    onSubmit={({ type, params, placement, count }) => launch({ type, params, placement, count })} />
        </div>
      )}
    </div>
  );
}

function Scoreboard({ board, compare }) {
  if (!board) return null;
  return (
    <div className="panel">
      <h3>This activity</h3>
      <div className="stats">
        <Stat label="jobs" value={board.total} />
        <Stat label="completed" value={board.completed} />
        {Object.entries(board.tiers).map(([tier, v]) => <Stat key={tier} label={`done on ${tier}`} value={v.completed} />)}
        <Stat label="offloaded (%)" value={board.offload_pct == null ? '–' : board.offload_pct} />
        <Stat label="waiting now" value={board.waiting} />
        <Stat label="longest queue" value={board.peak_queue} />
        <Stat label="95% finished within" value={board.p95_s == null ? '–' : `${board.p95_s} s`} />
        <Stat label="failed / killed" value={board.failed} />
      </div>
      {compare.length > 1 && (
        <>
          <h3 style={{ marginTop: 12 }}>Compare recent activities</h3>
          <table>
            <thead><tr><th>activity</th><th>policy</th><th>jobs</th><th>95% within</th><th>had to wait</th>
              <th>queue demand s</th><th>offloaded %</th></tr></thead>
            <tbody>{compare.map((c) => (
              <tr key={c.run_id}><td>{c.title}</td><td>{c.policy}</td><td>{c.completed}/{c.total}</td>
                <td>{c.p95_s == null ? '–' : `${c.p95_s} s`}</td><td>{c.overflow_jobs}</td>
                <td>{fmtNum(c.overflow_demand_s, 1)}</td><td>{c.offload_pct ?? '–'}</td></tr>
            ))}</tbody>
          </table>
        </>
      )}
    </div>
  );
}

export default function Playground() {
  const catalog = usePoll('/playground/scenarios', 30000);
  const { data, feed, error } = useLive();
  const [why, setWhy] = useState([]);           // job ids with an open "Why here?" card, newest first
  const [msg, setMsg] = useState(null);
  const [flashes, setFlashes] = useState({});   // job_id -> true while the arrival flash shows
  const prevQueue = useRef(new Set());
  const feedEnd = useRef(null);

  useEffect(() => {
    if (!data) return undefined;
    const inSlots = data.workers.flatMap((w) => [...w.slots.map((s) => s.job).filter(Boolean), ...w.arriving]);
    const arrived = inSlots.filter((t) => prevQueue.current.has(t.job_id)).map((t) => t.job_id);
    prevQueue.current = new Set(data.queue.map((q) => q.job_id));
    if (!arrived.length) return undefined;
    const id = setTimeout(() => {
      setFlashes((f) => ({ ...f, ...Object.fromEntries(arrived.map((j) => [j, true])) }));
    }, 0);
    const off = setTimeout(() => {
      setFlashes((f) => Object.fromEntries(Object.entries(f).filter(([j]) => !arrived.includes(j))));
    }, FLASH_MS);
    return () => { clearTimeout(id); clearTimeout(off); };
  }, [data]);

  useEffect(() => { feedEnd.current?.scrollIntoView({ block: 'nearest' }); }, [feed.length]);

  const openWhy = (ids) => setWhy((w) => [...ids, ...w.filter((x) => !ids.includes(x))].slice(0, 6));
  const closeWhy = (id) => setWhy((w) => w.filter((x) => x !== id));

  if (!catalog.data || !data) return <section><ErrorLine error={catalog.error || error} /><div className="dim">loading…</div></section>;
  return (
    <section className="playground">
      <ErrorLine error={error || msg} />
      <ActivityBar catalog={catalog.data} activity={data.activity} onError={setMsg} />
      <div className="stage">
        {data.workers.map((w) => (
          <WorkerCard key={w.worker_id} w={w} flashes={flashes} labels={data.labels} onWhy={(j) => openWhy([j])} />
        ))}
        <QueueColumn queue={data.queue} labels={data.labels} board={data.scoreboard} onWhy={(j) => openWhy([j])} />
      </div>
      <div className="pg-bottom">
        <div>
          <TaskPalette tasks={catalog.data.tasks} onLaunched={openWhy} onError={setMsg} />
          {why.length > 0 && <div className="whys">{why.map((j) => <WhyCard key={j} jobId={j} onClose={closeWhy} />)}</div>}
        </div>
        <div className="panel story">
          <h3>What is happening</h3>
          <div className="feed">
            {feed.map((f, i) => (
              <div key={`${f.ts}-${i}`} className="feed-line" onClick={() => f.job_id && openWhy([f.job_id])}>
                <span className="dim">{new Date(f.ts * 1000).toLocaleTimeString()}</span> {f.text}
              </div>
            ))}
            <div ref={feedEnd} />
          </div>
        </div>
      </div>
      <Scoreboard board={data.scoreboard} compare={data.compare} />
    </section>
  );
}
