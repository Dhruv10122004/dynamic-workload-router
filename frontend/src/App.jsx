import { useState } from 'react';
import { getToken, setToken, usePoll } from './api.js';
import Playground from './views/Playground.jsx';
import Overview from './views/Overview.jsx';
import Workers from './views/Workers.jsx';
import Jobs from './views/Jobs.jsx';
import Decisions from './views/Decisions.jsx';
import Submit from './views/Submit.jsx';

const VIEWS = { Playground, Overview, Workers, Jobs, Decisions, Submit };

// '#Playground/present' = presenter mode: large type, no navigation (projector).

export default function App() {
  const [view, setView] = useState(() => {
    const h = window.location.hash.slice(1).split('/')[0];
    return VIEWS[h] ? h : 'Playground';
  });
  const present = view === 'Playground' && window.location.hash.endsWith('/present');
  const health = usePoll('/health');
  const sched = usePoll('/scheduler', 5000);
  const [token, setTok] = useState(getToken());
  const View = VIEWS[view];
  const go = (v) => { setView(v); window.history.replaceState(null, '', `#${v}`); };
  const down = health.error && !health.data;
  const stale = health.error && health.data;

  return (
    <div className={`app ${present ? 'present' : ''}`}>
      <header className="bar">
        <div className="brand">Dynamic Workload Router</div>
        <nav hidden={present}>
          {Object.keys(VIEWS).map((v) => (
            <button key={v} className={v === view ? 'tab on' : 'tab'} onClick={() => go(v)}>{v}</button>
          ))}
        </nav>
        <div className="status">
          {down && <span className="badge bad">controller unreachable</span>}
          {stale && <span className="badge warn" title={health.error}>controller not answering, showing last data</span>}
          {health.data && (
            <span className={`badge ${health.data.status === 'READY' ? 'good' : 'warn'}`}
                  title={(health.data.reasons || []).join(', ')}>
              {health.data.status} · fresh: {(health.data.fresh_workers || []).join(', ') || 'none'}
            </span>
          )}
          {sched.data && (
            <span className="badge" title="active scheduler (GET /scheduler)">
              {sched.data.policy} · host-busy {sched.data.host_busy_enabled ? 'on' : 'off'}
            </span>
          )}
          <input hidden={present} className="token" type="password" placeholder="admin token (remote only)" value={token}
                 onChange={(e) => { setTok(e.target.value); setToken(e.target.value); }} />
        </div>
      </header>
      <main>
        <View />
      </main>
    </div>
  );
}
