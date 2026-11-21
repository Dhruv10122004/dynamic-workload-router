import { useEffect, useRef, useState } from 'react';
import { api, fmtNum, usePoll } from '../api.js';
import Icon from '../components/Icons.jsx';
import { KIND_ICON } from '../components/kindIcon.js';
import Sparkline from '../components/Sparkline.jsx';
import TaskForm from '../components/TaskForm.jsx';
import { ErrorLine, Stat } from './common.jsx';
import NetworkMap from './NetworkMap.jsx';

// Classroom playground (DECISIONS D-027, D-030). Every label, explanation and story line comes
// from /api/v1/playground/*; this file only lays them out and animates real changes.

const FLASH_MS = 3500;
const TOAST_MS = 6000;
const HISTORY_S = 120;
const TOAST_KINDS = new Set(['host_busy', 'host_free', 'worker_stale', 'worker_fresh']);
const clock = (ts) => new Date(ts * 1000).toLocaleTimeString([], { hour12: false });

// "what this means" line under a heading; dismissal is a per-viewer convenience only
function Hint({ id, text }) {
  const key = `dwr.hint.${id}`;
  const [hidden, setHidden] = useState(() => { try { return localStorage.getItem(key) === '1'; } catch { return false; } });
  if (!text || hidden) return null;
  const hide = () => { setHidden(true); try { localStorage.setItem(key, '1'); } catch { /* private window */ } };
  return (
    <div className="hint-line"><Icon name="info" size={13} /><span>{text}</span>
      <button className="btn tiny ghost" onClick={hide} aria-label="hide hint">×</button></div>
  );
}

// integer that counts towards the real value (the final number is always the API's)
function Num({ value }) {
  const [shown, setShown] = useState(value);
  const from = useRef(value);
  useEffect(() => {
    if (typeof value !== 'number' || typeof from.current !== 'number') { from.current = value; return undefined; }
    const a = from.current;
    const t0 = performance.now();
    let id = 0;
    const step = (t) => {
      const k = Math.min(1, (t - t0) / 600);
      setShown(k >= 1 ? value : Math.round(a + (value - a) * k));
      if (k < 1) id = requestAnimationFrame(step); else from.current = value;
    };
    id = requestAnimationFrame(step);
    return () => { cancelAnimationFrame(id); from.current = value; };
  }, [value]);
  return <>{typeof value === 'number' ? shown : value}</>;
}

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

function Now({ catalog, activity, onStart, onStop }) {
  const [custom, setCustom] = useState({ load: 1.5, mix: 'mixed', policy: 'LocalFirst', duration_s: 60 });
  const [showCustom, setShowCustom] = useState(false);
  const running = activity && activity.running;
  const lim = catalog.custom_limits;
  const pct = activity && activity.total ? (100 * activity.submitted) / activity.total : 0;
  return (
    <div className="now">
      <div className="now-head">
        <span className="eyebrow">{running ? 'Running now' : activity ? 'Last activity' : 'No activity yet'}</span>
        {running && <button className="btn danger" onClick={onStop}>Stop</button>}
      </div>
      {activity ? (
        <>
          <div className="now-title">{activity.title}</div>
          <div className="now-policy">{activity.policy_text}</div>
          <div className="now-watch">{activity.watch}</div>
          {activity.total > 0 && (
            <div className="progress" title="jobs submitted so far">
              <div style={{ width: `${pct}%` }} />
              <span>{activity.submitted}/{activity.total} jobs ·{' '}
                {running ? `${fmtNum(activity.elapsed_s, 0)} s${activity.duration_s ? ` of ${activity.duration_s} s` : ''}`
                  : `finished · took ${fmtNum(activity.elapsed_s, 0)} s`}</span>
            </div>
          )}
        </>
      ) : <div className="now-watch">Pick an activity on the left, or launch single tasks below.</div>}
      {activity && activity.cue && <div className="cue">{activity.cue}</div>}
      <button className="btn ghost wide" onClick={() => setShowCustom(!showCustom)}>
        {showCustom ? 'Hide custom activity' : 'Custom activity…'}
      </button>
      {showCustom && (
        <div className="custom">
          <label>Load <b>{custom.load}×</b>
            <input type="range" min={lim.load[0]} max={lim.load[1]} step="0.25" value={custom.load}
                   onChange={(e) => setCustom({ ...custom, load: parseFloat(e.target.value) })} />
          </label>
          <label>Duration <b>{custom.duration_s} s</b>
            <input type="range" min={lim.duration_s[0]} max={lim.duration_s[1]} step="10" value={custom.duration_s}
                   onChange={(e) => setCustom({ ...custom, duration_s: parseInt(e.target.value, 10) })} />
          </label>
          <label>Job mix
            <select value={custom.mix} onChange={(e) => setCustom({ ...custom, mix: e.target.value })}>
              {catalog.mixes.map((m) => <option key={m}>{m}</option>)}
            </select>
          </label>
          <label>Policy
            <select value={custom.policy} onChange={(e) => setCustom({ ...custom, policy: e.target.value })}>
              {catalog.policies.map((p) => <option key={p}>{p}</option>)}
            </select>
          </label>
          <button className="btn primary wide" disabled={running} onClick={() => onStart({ custom })}>Start custom</button>
        </div>
      )}
    </div>
  );
}

function Activities({ catalog, activity, onError }) {
  const running = activity && activity.running;
  const start = async (body) => {
    try { await api('/playground/activity', { method: 'POST', body }); onError(null); } catch (e) { onError(e.message); }
  };
  const stop = async () => {
    try { await api('/playground/activity/stop', { method: 'POST' }); } catch (e) { onError(e.message); }
  };
  return (
    <div className="panel activities">
      <div className="presets">
        <h3>Activities</h3>
        <div className="preset-grid">
          {catalog.scenarios.map((s) => {
            const on = running && activity.title === s.title;
            return (
              <button key={s.id} className={`preset ${on ? 'on' : ''}`} disabled={running} title={s.watch}
                      onClick={() => start({ scenario: s.id })}>
                <span className="preset-title">{s.title}</span>
                <span className="preset-text">{s.watch}</span>
                {s.needs && <span className="tag" title={s.needs}>{s.needs_short}</span>}
              </button>
            );
          })}
        </div>
      </div>
      <Now catalog={catalog} activity={activity} onStart={start} onStop={stop} />
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
      <div className="tile-head">
        <span className="tile-n">#{t.n}</span><span className="tile-label">{t.label}</span>
        {!t.offline && !t.cancel_requested && t.state === 'RUNNING' &&
          <button className="btn tiny danger" onClick={cancel}>cancel</button>}
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

function WorkerCard({ w, accent, focused, flashes, labels, onWhy }) {
  return (
    <div id={`card-${w.worker_id}`}
         className={`pg-card has-acc acc-${accent}${w.fresh ? '' : 'is-stale'} ${w.host_busy ? 'is-busy' : ''} ${focused ? 'focus' : ''}`}>
      <div className="card-head">
        <span className="acc-dot" /><Icon name="laptop" size={16} />
        <span className="wid">{w.worker_id}</span><span className="tier">{w.tier}</span>
        <span className="badges">
          {w.fresh ? <span className="badge good">online</span> : <span className="badge bad">STALE</span>}
          {w.host_busy && <span className="badge warn">HOST BUSY</span>}
          {w.draining && <span className="badge warn">DRAINING</span>}
        </span>
      </div>
      <div className="sub">{w.hostname} · {w.os} · CPU {fmtNum(w.cpu_pct, 0)}% · RAM {fmtNum(w.mem_pct, 0)}%
        {!w.fresh && w.heartbeat_age_s != null && <span className="bad-text"> · no heartbeat for {fmtNum(w.heartbeat_age_s, 0)} s</span>}
      </div>
      {w.slots.map((s) => (
        <div key={s.slot} className="lane">
          <div className="lane-label">slot {s.slot}<span>core {s.cpus.join('+')}</span></div>
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

function QueueCard({ queue, labels, board, history, onWhy }) {
  return (
    <div className="pg-card queue">
      <div className="card-head"><Icon name="queue" size={16} /><span className="wid">{labels.queue_title}</span>
        <span className="badges"><span className={`badge ${queue.length ? 'warn' : ''}`}>{queue.length} waiting</span></span></div>
      <div className="sub">{labels.queue_note}</div>
      <Sparkline points={history} width={260} height={36} label="queue length, last 2 min" />
      {board && <div className="demand">{labels.demand_label}: <b>{fmtNum(board.overflow_demand_s, 1)} s</b>
        <span className="dim"> · {board.overflow_jobs} jobs had to wait</span></div>}
      {queue.length === 0 ? <div className="idle">{labels.queue_empty}</div> : (
        <div className="chips">
          {queue.map((q) => (
            <div key={q.job_id} className="chip" onClick={() => onWhy(q.job_id)}>
              <span><b>#{q.n}</b> {q.label}</span><span className="dim">{fmtNum(q.waiting_s, 0)} s</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function WhyCard({ jobId, onClose }) {
  const { data } = usePoll(`/playground/explain/${jobId}`, 1000);
  if (!data) return null;
  return (
    <div className={`why ${data.clicked ? 'clicked' : ''}`}>
      <button className="btn tiny ghost close" onClick={() => onClose(jobId)} aria-label="close">×</button>
      <div className="why-head">{data.headline}</div>
      {data.lines.length > 0 && <ul>{data.lines.map((l) => <li key={l}>{l}</li>)}</ul>}
      <div className="timeline">{data.timeline.map((e) => (
        <span key={`${e.t}${e.state}`} className={`state ${e.state}`}>{e.state} +{e.t}s</span>
      ))}</div>
    </div>
  );
}

function TaskPanel({ tasks, hint, why, onLaunched, onClose, onError }) {
  const [custom, setCustom] = useState(false);
  const launch = async (body) => {
    const r = await api('/playground/task', { method: 'POST', body });
    onLaunched(r.jobs.map((j) => j.job_id));
  };
  const click = async (id) => {
    try { await launch({ task_id: id }); onError(null); } catch (e) { onError(e.message); }
  };
  return (
    <div className="panel col">
      <h3><Icon name="play" size={15} /> Launch a task</h3>
      <Hint id="tasks" text={hint} />
      <div className="palette">
        {tasks.map((t) => (
          <button key={t.id} className="btn task" onClick={() => click(t.id)}>
            <span className="task-label">{t.label}</span><span className="task-hint">{t.hint}</span>
          </button>
        ))}
      </div>
      <button className="btn ghost wide" onClick={() => setCustom(!custom)}>{custom ? 'Hide task builder' : 'Build a custom task…'}</button>
      {custom && (
        <div className="builder">
          <TaskForm maxCount={10} showTimeout={false} submitLabel="Launch"
                    onSubmit={({ type, params, placement, count }) => launch({ type, params, placement, count })} />
        </div>
      )}
      {why.length > 0 ? <div className="whys">{why.map((j) => <WhyCard key={j} jobId={j} onClose={onClose} />)}</div>
        : <div className="hint">Click a task, a tile or a queued job to see why it went where it did.</div>}
    </div>
  );
}

function Story({ feed, hint, accents, onWhy }) {
  const box = useRef(null);
  useEffect(() => {          // keep the newest line in view without scrolling the page
    if (box.current) box.current.scrollTop = box.current.scrollHeight;
  }, [feed.length]);
  return (
    <div className="panel col">
      <h3><Icon name="clock" size={15} /> What is happening</h3>
      <Hint id="story" text={hint} />
      <div className="feed" ref={box}>
        {feed.length === 0 && <div className="hint">Nothing yet: start an activity or launch a task.</div>}
        {feed.map((f, i) => {
          const acc = f.worker_id != null && accents[f.worker_id] != null ? `has-acc acc-${accents[f.worker_id]}` : '';
          return (
            <div key={`${f.ts}-${i}`} className={`feed-line k-${f.kind || 'event'} ${acc} ${f.job_id ? 'link' : 'event'}`}
                 onClick={() => f.job_id && onWhy([f.job_id])}>
              <span className="time">{clock(f.ts)}</span>
              <span className="feed-icon"><Icon name={KIND_ICON[f.kind] || 'info'} size={13} /></span>
              <span>{f.text}</span>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function Tally({ board, hint, history }) {
  return (
    <div className="panel col">
      <h3><Icon name="cpu" size={15} /> This activity</h3>
      <Hint id="tally" text={hint} />
      {!board ? <div className="hint">Numbers appear once an activity or task runs.</div> : (
        <>
          <div className="tally-grid">
            <Stat label="completed" value={<><Num value={board.completed} />/{board.total}</>} />
            {Object.entries(board.tiers).map(([tier, v]) => <Stat key={tier} label={`done on ${tier}`} value={<Num value={v.completed} />} />)}
            <Stat label="offloaded" value={board.offload_pct == null ? '–' : `${board.offload_pct}%`} />
            <Stat label="waiting now" value={<Num value={board.waiting} />} />
            <Stat label="longest queue" value={<Num value={board.peak_queue} />} />
            <Stat label="95% finished within" value={board.p95_s == null ? '–' : `${board.p95_s} s`} />
            <Stat label="failed / killed" value={<Num value={board.failed} />} />
          </div>
          <Sparkline points={history} width={300} height={40} label="queue length, last 2 min" />
        </>
      )}
    </div>
  );
}

function Toasts({ toasts }) {
  return (
    <div className="toasts" aria-live="polite">
      {toasts.map((t) => (
        <div key={t.key} className={`toast k-${t.kind}`}><Icon name={KIND_ICON[t.kind] || 'info'} size={16} /><span>{t.text}</span></div>
      ))}
    </div>
  );
}

function Compare({ compare }) {
  if (compare.length < 2) return null;
  return (
    <div className="panel">
      <h3>Compare recent activities</h3>
      <div className="scroll">
        <table className="grid">
          <thead><tr><th>activity</th><th>policy</th><th className="num">jobs</th><th className="num">95% within</th>
            <th className="num">had to wait</th><th className="num">queue demand</th><th className="num">offloaded</th></tr></thead>
          <tbody>{compare.map((c) => (
            <tr key={c.run_id}><td>{c.title}</td><td>{c.policy}</td><td className="num">{c.completed}/{c.total}</td>
              <td className="num">{c.p95_s == null ? '–' : `${c.p95_s} s`}</td><td className="num">{c.overflow_jobs}</td>
              <td className="num">{fmtNum(c.overflow_demand_s, 1)} s</td>
              <td className="num">{c.offload_pct == null ? '–' : `${c.offload_pct}%`}</td></tr>
          ))}</tbody>
        </table>
      </div>
    </div>
  );
}

export default function Playground() {
  const catalog = usePoll('/playground/scenarios', 30000);
  const { data, feed, error } = useLive();
  const [why, setWhy] = useState([]);           // job ids with an open "Why here?" card, newest first
  const [msg, setMsg] = useState(null);
  const [flashes, setFlashes] = useState({});   // job_id -> true while the arrival flash shows
  const [history, setHistory] = useState([]);   // [{t, v}] real queue lengths of the current activity
  const [toasts, setToasts] = useState([]);
  const [focus, setFocus] = useState(null);
  const prevQueue = useRef(new Set());
  const prevRun = useRef(undefined);
  const prevRunning = useRef(undefined);
  const timers = useRef([]);
  const later = (fn, ms) => { timers.current.push(setTimeout(fn, ms)); };

  useEffect(() => () => timers.current.forEach(clearTimeout), []);

  useEffect(() => {
    if (!data) return;
    // arrival flash: a job in a lane that was in the queue on the previous poll
    const inSlots = data.workers.flatMap((w) => [...w.slots.map((s) => s.job).filter(Boolean), ...w.arriving]);
    const arrived = inSlots.filter((t) => prevQueue.current.has(t.job_id)).map((t) => t.job_id);
    prevQueue.current = new Set(data.queue.map((q) => q.job_id));
    if (arrived.length) {
      later(() => setFlashes((f) => ({ ...f, ...Object.fromEntries(arrived.map((j) => [j, true])) })), 0);
      later(() => setFlashes((f) => Object.fromEntries(Object.entries(f).filter(([j]) => !arrived.includes(j)))), FLASH_MS);
    }
    // queue-length history, reset when the activity changes
    const a = data.activity;
    const run = a ? a.run_id : null;
    const reset = run !== prevRun.current;
    prevRun.current = run;
    const point = { t: data.now, v: data.queue.length };
    later(() => setHistory((h) => (reset ? [point] : [...h, point].filter((p) => p.t >= data.now - HISTORY_S))), 0);
    // toasts: laptop events from the story feed, and an activity starting or finishing
    const news = data.feed.filter((f) => TOAST_KINDS.has(f.kind)).map((f) => ({ kind: f.kind, text: f.text }));
    const running = a ? a.running : null;
    if (prevRunning.current !== undefined && a && running !== prevRunning.current) {
      news.push(running ? { kind: 'place', text: `Started: ${a.title}` }
        : { kind: 'done', text: `Finished: ${a.title} (${fmtNum(a.elapsed_s, 0)} s)` });
    }
    prevRunning.current = running;
    if (news.length) {
      const made = news.slice(-3).map((n, i) => ({ ...n, key: `${data.now}-${i}` }));
      const keys = new Set(made.map((m) => m.key));
      later(() => setToasts((t) => [...t, ...made].slice(-4)), 0);
      later(() => setToasts((t) => t.filter((x) => !keys.has(x.key))), TOAST_MS);
    }
    if (timers.current.length > 400) timers.current = timers.current.slice(-200);
  }, [data]);

  const openWhy = (ids) => setWhy((w) => [...ids, ...w.filter((x) => !ids.includes(x))].slice(0, 4));
  const closeWhy = (id) => setWhy((w) => w.filter((x) => x !== id));
  const focusWorker = (id) => {
    const el = document.getElementById(`card-${id}`);
    if (el) el.scrollIntoView({ behavior: 'smooth', block: 'center' });
    setFocus(id);
    later(() => setFocus((f) => (f === id ? null : f)), 1800);
  };

  if (!catalog.data || !data) {
    return <section><ErrorLine error={catalog.error || error} /><div className="hint">Loading…</div></section>;
  }
  const hints = catalog.data.hints || {};
  const accents = Object.fromEntries(data.workers.map((w, i) => [w.worker_id, i % 4]));
  return (
    <section className="playground">
      <ErrorLine error={error || msg} />
      <Toasts toasts={toasts} />
      <Activities catalog={catalog.data} activity={data.activity} onError={setMsg} />
      <div className="panel map-panel">
        <h3><Icon name="server" size={15} /> Live map</h3>
        <Hint id="map" text={hints.map} />
        <NetworkMap live={data} labels={data.labels} legend={catalog.data.legend || []}
                    onWhy={(j) => openWhy([j])} onFocusWorker={focusWorker} />
      </div>
      <Hint id="lanes" text={hints.lanes} />
      <div className="stage">
        {data.workers.map((w) => (
          <WorkerCard key={w.worker_id} w={w} accent={accents[w.worker_id]} focused={focus === w.worker_id}
                      flashes={flashes} labels={data.labels} onWhy={(j) => openWhy([j])} />
        ))}
        <QueueCard queue={data.queue} labels={data.labels} board={data.scoreboard} history={history}
                   onWhy={(j) => openWhy([j])} />
      </div>
      <div className="pg-cols">
        <TaskPanel tasks={catalog.data.tasks} hint={hints.tasks} why={why} onLaunched={openWhy} onClose={closeWhy} onError={setMsg} />
        <Story feed={feed} hint={hints.story} accents={accents} onWhy={openWhy} />
        <Tally board={data.scoreboard} hint={hints.tally} history={history} />
      </div>
      <Compare compare={data.compare} />
    </section>
  );
}
