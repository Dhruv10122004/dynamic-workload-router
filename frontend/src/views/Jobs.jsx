import { useState } from 'react';
import { TERMINAL, api, fmtNum, fmtTime, short, usePoll } from '../api.js';
import { ErrorLine, StateCell } from './common.jsx';

const STATES = ['', 'PENDING', 'ASSIGNED', 'ACCEPTED', 'RUNNING', 'UNKNOWN', 'SUCCEEDED', 'FAILED', 'KILLED',
  'CANCELLED', 'REJECTED'];

export default function Jobs() {
  const [state, setState] = useState('');
  const [limit, setLimit] = useState(200);
  const q = `/jobs?order=desc&limit=${limit}${state ? `&state=${state}` : ''}`;
  const { data, error } = usePoll(q);
  const [msg, setMsg] = useState(null);
  const cancel = async (id) => {
    try {
      const r = await api(`/jobs/${id}/cancel`, { method: 'POST' });
      setMsg(`cancel requested for ${short(id)} (now ${r.state})`);
    } catch (e) { setMsg(e.message); }
  };
  return (
    <section>
      <div className="filters">
        <label>state <select value={state} onChange={(e) => setState(e.target.value)}>
          {STATES.map((s) => <option key={s} value={s}>{s || 'all'}</option>)}
        </select></label>
        <label>show <select value={limit} onChange={(e) => setLimit(Number(e.target.value))}>
          {[50, 200, 1000].map((n) => <option key={n} value={n}>{n} newest</option>)}
        </select></label>
        {msg && <span className="note">{msg}</span>}
      </div>
      <ErrorLine error={error} />
      <div className="scroll">
        <table className="grid">
          <thead>
            <tr><th>job</th><th>type</th><th>worker</th><th>host</th><th>pid</th><th>state</th><th>submit</th>
              <th>start</th><th>end</th><th>exec_s</th><th>transfer_s</th><th>completion_s</th><th>overflow</th><th></th></tr>
          </thead>
          <tbody>
            {(data || []).map((j) => (
              <tr key={j.job_id}>
                <td className="mono" title={j.job_id}>{short(j.job_id)}</td>
                <td>{j.type}</td>
                <td>{j.worker_id}</td>
                <td>{j.hostname}</td>
                <td className="mono">{j.pid}</td>
                <td><StateCell job={j} />{j.cancel_requested && !TERMINAL.has(j.state) && <span className="dim"> (cancel requested)</span>}</td>
                <td>{fmtTime(j.submit_ts)}</td>
                <td>{fmtTime(j.start_ts)}</td>
                <td>{fmtTime(j.end_ts)}</td>
                <td className="num">{fmtNum(j.exec_s, 3)}</td>
                <td className="num">{fmtNum(j.transfer_s, 3)}</td>
                <td className="num">{fmtNum(j.completion_s, 2)}</td>
                <td>{j.overflow ? 'yes' : ''}</td>
                <td>{!TERMINAL.has(j.state) && !j.cancel_requested &&
                  <button className="small" onClick={() => cancel(j.job_id)}>cancel</button>}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {data && data.length === 0 && <div className="dim">No jobs.</div>}
      </div>
    </section>
  );
}
