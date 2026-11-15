import { fmtNum, fmtTime, short, usePoll } from '../api.js';
import { ErrorLine, Stat } from './common.jsx';

export default function Overview() {
  const ov = usePoll('/overview');
  const ev = usePoll('/events?order=desc&limit=40', 2000);
  const o = ov.data;
  return (
    <section>
      <ErrorLine error={ov.error} />
      {o && (
        <>
          <div className="stats">
            <Stat label="jobs" value={o.total_jobs} />
            <Stat label="completed" value={o.completed} />
            <Stat label="backlog (PENDING)" value={o.backlog} />
            <Stat label="peak backlog" value={o.peak_backlog} />
            <Stat label="overflow jobs" value={o.overflow_jobs}
                  hint="no eligible worker at the first scheduling attempt" />
            <Stat label="overflow demand" value={`${fmtNum(o.overflow_demand_s, 1)} s`}
                  hint="sum of exec_s of the overflow jobs (approximate slot-seconds a scale-out tier would have needed)" />
            <Stat label="overflow time" value={`${fmtNum(o.overflow_time_s, 1)} s`}
                  hint="time the PENDING queue was non-empty" />
            <Stat label="local offload (count)" value={o.local_offload_pct_by_count === null ? '–' : `${o.local_offload_pct_by_count}%`}
                  hint={`completed jobs not on the first tier (${o.first_tier ?? 'none'})`} />
            <Stat label="local offload (CPU-s)" value={o.local_offload_pct_by_cpu_s === null ? '–' : `${o.local_offload_pct_by_cpu_s}%`}
                  hint="share of CPU-seconds done away from the first tier" />
          </div>

          <div className="cols">
            <div className="panel">
              <h3>Jobs by state</h3>
              <table>
                <tbody>
                  {Object.entries(o.by_state).map(([s, n]) => (
                    <tr key={s}><td><span className={`state ${s}`}>{s}</span></td><td className="num">{n}</td></tr>
                  ))}
                  {Object.keys(o.by_state).length === 0 && <tr><td className="dim">no jobs yet</td></tr>}
                </tbody>
              </table>
            </div>
            <div className="panel">
              <h3>Placement by tier</h3>
              <table>
                <thead><tr><th>tier</th><th>placed</th><th>completed</th><th>CPU-s</th></tr></thead>
                <tbody>
                  {Object.entries(o.tiers).map(([t, v]) => (
                    <tr key={t}><td>{t}</td><td className="num">{v.placed}</td><td className="num">{v.completed}</td>
                      <td className="num">{fmtNum(v.cpu_s, 1)}</td></tr>
                  ))}
                  {Object.keys(o.tiers).length === 0 && <tr><td className="dim" colSpan={4}>nothing placed yet</td></tr>}
                </tbody>
              </table>
            </div>
          </div>
        </>
      )}

      <div className="panel">
        <h3>Events</h3>
        <ErrorLine error={ev.error} />
        <table>
          <thead><tr><th>time</th><th>kind</th><th>worker</th><th>job</th><th>details</th></tr></thead>
          <tbody>
            {(ev.data || []).map((e) => (
              <tr key={e.id} className={`ev ${e.kind}`}>
                <td>{fmtTime(e.ts)}</td><td>{e.kind}</td><td>{e.worker_id}</td><td className="mono">{short(e.job_id)}</td>
                <td className="mono small">{e.details ? JSON.stringify(e.details) : ''}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
