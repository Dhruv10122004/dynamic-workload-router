// Turns two consecutive /playground/live payloads into the moves the network map animates
// (DECISIONS D-030). Pure: no DOM, no timers. Nothing is invented: every move is a job that
// really changed place between the two polls.

export const CONTROLLER = '__controller';
export const QUEUE = '__queue';

const FINAL_KIND = { SUCCEEDED: 'done', CANCELLED: 'cancel' };   // anything else terminal = 'fail'

// job_id -> where it was: a worker_id, QUEUE, or 'done'
export function placesOf(live) {
  const at = new Map();
  if (!live) return at;
  for (const w of live.workers) {
    for (const s of w.slots) if (s.job) at.set(s.job.job_id, w.worker_id);
    for (const t of w.arriving) at.set(t.job_id, w.worker_id);
  }
  for (const q of live.queue) at.set(q.job_id, QUEUE);
  return at;
}

// -> [{ job_id, n, from, to, kind }], kind in place | queue | pick | done | fail | cancel.
// `from === to` means "changes colour where it is" (a job that finished in its lane).
export function diffLive(prev, next, limit = 30) {
  if (!prev || !next) return [];
  const before = placesOf(prev);
  const after = placesOf(next);
  const prevFinished = new Set(prev.finished.map((f) => f.job_id));
  const label = new Map();
  for (const w of next.workers) {
    for (const s of w.slots) if (s.job) label.set(s.job.job_id, s.job.n);
    for (const t of w.arriving) label.set(t.job_id, t.n);
  }
  for (const q of next.queue) label.set(q.job_id, q.n);
  for (const f of next.finished) label.set(f.job_id, f.n);

  const moves = [];
  for (const [job, to] of after) {
    const from = before.get(job);
    if (from === to) continue;
    if (to === QUEUE) moves.push({ job_id: job, from: CONTROLLER, to, kind: 'queue' });
    else if (from === QUEUE) moves.push({ job_id: job, from: QUEUE, to, kind: 'pick' });
    else if (from === undefined) moves.push({ job_id: job, from: CONTROLLER, to, kind: 'place' });
  }
  for (const f of next.finished) {
    if (prevFinished.has(f.job_id) || !f.worker_id) continue;
    const kind = FINAL_KIND[f.state] || 'fail';
    const from = before.get(f.job_id);
    if (from === f.worker_id) moves.push({ job_id: f.job_id, from: f.worker_id, to: f.worker_id, kind });
    else moves.push({ job_id: f.job_id, from: from === QUEUE ? QUEUE : CONTROLLER, to: f.worker_id, kind });
  }
  return moves.slice(0, limit).map((m) => ({ ...m, n: label.get(m.job_id) }));
}
