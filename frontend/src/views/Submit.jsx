import { useState } from 'react';
import { api, short } from '../api.js';
import TaskForm from '../components/TaskForm.jsx';

export default function Submit() {
  const [result, setResult] = useState(null);

  const submit = async ({ type, params, placement, count, timeout_s }) => {
    setResult(null);
    const body = { type, params, placement };
    if (timeout_s !== undefined) body.timeout_s = timeout_s;
    const ids = [];
    for (let i = 0; i < count; i += 1) ids.push((await api('/jobs', { method: 'POST', body })).job_id);
    setResult(ids);
  };

  return (
    <section className="panel narrow">
      <TaskForm onSubmit={submit} />
      {result && <div className="note">submitted {result.length}: {result.map(short).join(', ')} (see Jobs)</div>}
    </section>
  );
}
