import { useMemo, useState } from 'react';
import { api, short, usePoll } from '../api.js';
import { ErrorLine } from './common.jsx';

// The form is generated from GET /catalog: types, bounds and defaults are never hardcoded.
export default function Submit() {
  const catalog = usePoll('/catalog', 60000);
  const workers = usePoll('/workers', 5000);
  const [chosen, setType] = useState('');
  const [edits, setEdits] = useState({});          // type -> {param: typed value}
  const [placement, setPlacement] = useState('auto');
  const [timeout, setTimeoutS] = useState('');
  const [count, setCount] = useState(1);
  const [showTest, setShowTest] = useState(false);
  const [result, setResult] = useState(null);
  const [error, setError] = useState(null);

  const types = useMemo(() => catalog.data || [], [catalog.data]);
  const type = chosen || (types[0] && types[0].type) || '';
  const spec = types.find((t) => t.type === type);
  // Typed values win; otherwise the catalog default (test-only params start empty).
  const values = spec ? Object.fromEntries(spec.params.map((p) => [
    p.name, (edits[type] || {})[p.name] ?? (p.test_only ? '' : String(p.default))])) : {};
  const setValues = (v) => setEdits({ ...edits, [type]: v });

  const submit = async (e) => {
    e.preventDefault();
    setError(null);
    setResult(null);
    const params = {};
    for (const p of spec.params) {
      const raw = values[p.name];
      if (raw === undefined || raw === '') continue;
      params[p.name] = p.kind === 'int' ? parseInt(raw, 10) : parseFloat(raw);
    }
    const body = { type, params, placement };
    if (timeout !== '') body.timeout_s = parseFloat(timeout);
    try {
      const ids = [];
      for (let i = 0; i < count; i += 1) ids.push((await api('/jobs', { method: 'POST', body })).job_id);
      setResult(ids);
    } catch (err) { setError(err.message); }
  };

  return (
    <section className="panel narrow">
      <ErrorLine error={catalog.error || error} />
      {spec && (
        <form onSubmit={submit}>
          <label>type
            <select value={type} onChange={(e) => setType(e.target.value)}>
              {types.map((t) => <option key={t.type} value={t.type}>{t.type}</option>)}
            </select>
          </label>
          <p className="dim">{spec.description}</p>
          {spec.params.filter((p) => showTest || !p.test_only).map((p) => (
            <label key={p.name}>{p.name}{p.test_only ? ' (test mode only)' : ''}
              <input type="number" min={p.min} max={p.max} step={p.kind === 'int' ? 1 : 'any'}
                     value={values[p.name] ?? ''} required={!p.test_only}
                     onChange={(e) => setValues({ ...values, [p.name]: e.target.value })} />
              <span className="dim">{p.min} – {p.max}, default {p.default}</span>
            </label>
          ))}
          <label>timeout_s (optional)
            <input type="number" min="0" step="any" value={timeout} onChange={(e) => setTimeoutS(e.target.value)} />
          </label>
          <label>placement
            <select value={placement} onChange={(e) => setPlacement(e.target.value)}>
              <option value="auto">auto (active policy)</option>
              {(workers.data || []).map((w) => (
                <option key={w.worker_id} value={w.worker_id}>{w.worker_id} ({w.tier}{w.fresh ? '' : ', stale'})</option>
              ))}
            </select>
          </label>
          <label>how many
            <input type="number" min="1" max="200" value={count} onChange={(e) => setCount(Math.max(1, parseInt(e.target.value, 10) || 1))} />
          </label>
          <label className="inline">
            <input type="checkbox" checked={showTest} onChange={(e) => setShowTest(e.target.checked)} /> show test-only parameters
          </label>
          <button type="submit">submit</button>
        </form>
      )}
      {result && <div className="note">submitted {result.length}: {result.map(short).join(', ')} (see Jobs)</div>}
    </section>
  );
}
