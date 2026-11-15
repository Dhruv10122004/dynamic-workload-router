export function ErrorLine({ error }) {
  return error ? <div className="error">{error}</div> : null;
}

export function Stat({ label, value, hint }) {
  return (
    <div className="stat" title={hint}>
      <div className="v">{value}</div>
      <div className="l">{label}</div>
    </div>
  );
}

export function StateCell({ job }) {
  const reason = job.fail_reason || job.kill_reason;
  return (
    <span className={`state ${job.state}`} title={job.error || ''}>
      {job.state}{reason ? `(${reason})` : ''}
    </span>
  );
}
