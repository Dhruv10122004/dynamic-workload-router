import { useState } from 'react';
import { api, fmtNum, usePoll } from '../api.js';
import { ErrorLine } from './common.jsx';

function pct(used, total) {
  return total ? `${((100 * used) / total).toFixed(0)}%` : '';
}

export default function Workers() {
  const { data, error } = usePoll('/workers');
  const [msg, setMsg] = useState(null);
  const act = async (id, what) => {
    try { await api(`/workers/${id}/${what}`, { method: 'POST' }); setMsg(null); } catch (e) { setMsg(e.message); }
  };
  return (
    <section>
      <ErrorLine error={error || msg} />
      <div className="cards">
        {(data || []).map((w) => {
          const t = w.telemetry || {};
          const memUsed = t.mem_total_mb ? t.mem_total_mb - t.mem_available_mb : null;
          return (
            <div key={w.worker_id} className={`card ${w.fresh ? '' : 'is-stale'} ${w.host_busy ? 'is-busy' : ''}`}>
              <div className="card-head">
                <span className="wid">{w.worker_id}</span>
                <span className="tier">{w.tier}</span>
                {w.fresh ? <span className="badge good">online</span>
                  : <span className="badge bad">STALE</span>}
                {w.host_busy && <span className="badge warn" title={`foreign CPU mean ${w.foreign_cpu_mean}%`}>HOST BUSY</span>}
                {w.draining && <span className="badge warn">DRAINING</span>}
              </div>
              <div className="sub">{w.hostname} · {w.os} {w.os_version} · Python {w.python} · agent {w.agent_version}</div>
              {!w.fresh && <div className="bad-text small">No heartbeat: the figures below are the last ones it reported.</div>}
              <table>
                <tbody>
                  <tr><td>last heartbeat</td>
                    <td className={w.fresh ? '' : 'bad-text'}>
                      {w.heartbeat_age_s === null ? 'never (since controller start)' : `${fmtNum(w.heartbeat_age_s, 1)} s ago`}
                    </td></tr>
                  <tr><td>CPU (host)</td><td>{fmtNum(t.cpu_pct, 1)}%</td></tr>
                  <tr><td>CPU (foreign)</td><td>{fmtNum(t.foreign_cpu_pct, 1)}% <span className="dim">(10-sample mean {w.foreign_cpu_mean ?? '–'}%)</span></td></tr>
                  <tr><td>CPU (DWR jobs)</td><td>{fmtNum(t.own_cpu_pct, 1)}%</td></tr>
                  <tr><td>RAM</td><td>{memUsed === null ? '' : `${pct(memUsed, t.mem_total_mb)} of ${t.mem_total_mb} MB (${t.mem_available_mb} MB available)`}</td></tr>
                  <tr><td>slots</td><td>{w.slots_total - w.slots_used} free / {w.slots_total}
                    <span className="dim"> · cores {w.slots.map((s) => s.cpus.join('+')).join(' | ')}</span></td></tr>
                  <tr><td>active jobs</td><td>{w.slots_used}</td></tr>
                  <tr><td>memory reserved</td><td>{w.mem_committed_mb} / {w.mem_budget_mb} MB (floor {w.mem_floor_mb} MB)</td></tr>
                  <tr><td>RTT</td><td>{t.rtt_ms_last === null || t.rtt_ms_last === undefined ? '' : `${fmtNum(t.rtt_ms_last, 1)} ms`}</td></tr>
                  <tr><td>types</td><td>{w.supported_types.join(', ')}</td></tr>
                  <tr><td>capabilities</td><td>
                    {Object.entries(w.capabilities).map(([k, v]) => (
                      <span key={k} className={`cap ${v === 'not_enforced' || v === false ? 'off' : ''}`}>{k}: {String(v)}</span>
                    ))}
                  </td></tr>
                </tbody>
              </table>
              <div className="actions">
                {w.draining
                  ? <button onClick={() => act(w.worker_id, 'enable')}>enable</button>
                  : <button onClick={() => act(w.worker_id, 'drain')}>drain</button>}
              </div>
            </div>
          );
        })}
        {data && data.length === 0 && <div className="dim">No worker has registered yet.</div>}
      </div>
    </section>
  );
}
