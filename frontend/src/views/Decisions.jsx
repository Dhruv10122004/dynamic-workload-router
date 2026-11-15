import { fmtNum, fmtTime, short, usePoll } from '../api.js';
import { ErrorLine } from './common.jsx';

export default function Decisions() {
  const { data, error } = usePoll('/decisions?limit=200');
  return (
    <section>
      <ErrorLine error={error} />
      <p className="dim">One row per placement or reject, plus one at a job's first attempt when it had to wait
        (that job is counted as overflow). Newest first.</p>
      <div className="scroll">
        <table className="grid">
          <thead>
            <tr><th>time</th><th>job</th><th>type</th><th>mem MB</th><th>placement</th><th>policy</th><th>chosen</th>
              <th>eligible</th><th>ineligible (reason)</th><th>decision µs</th><th>job now</th></tr>
          </thead>
          <tbody>
            {(data || []).map((d) => (
              <tr key={d.id} className={d.structural_reject ? 'rejected' : d.worker_id ? '' : 'waiting'}>
                <td>{fmtTime(d.ts)}</td>
                <td className="mono" title={d.job_id}>{short(d.job_id)}</td>
                <td>{d.type}</td>
                <td className="num">{d.mem_mb}</td>
                <td>{d.placement}</td>
                <td>{d.policy}</td>
                <td>{d.worker_id || (d.structural_reject ? 'REJECTED' : 'waits')}</td>
                <td>{d.eligible.join(', ')}</td>
                <td>{Object.entries(d.ineligible).map(([w, r]) => (
                  <span key={w} className="reason">{w}: {r}</span>
                ))}</td>
                <td className="num">{fmtNum(d.decision_us, 0)}</td>
                <td><span className={`state ${d.job_state}`}>{d.job_state}</span></td>
              </tr>
            ))}
          </tbody>
        </table>
        {data && data.length === 0 && <div className="dim">No decisions yet.</div>}
      </div>
    </section>
  );
}
