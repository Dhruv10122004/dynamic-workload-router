// All data comes from the controller's /api/v1 on the same origin (plan §10).
// Admin endpoints need no token on the controller's own machine; from elsewhere set the admin token in the header bar.
import { useEffect, useRef, useState } from 'react';

const BASE = '/api/v1';
const TOKEN_KEY = 'dwr-admin-token';

export function getToken() {
  try { return localStorage.getItem(TOKEN_KEY) || ''; } catch { return ''; }
}

export function setToken(t) {
  try {
    if (t) localStorage.setItem(TOKEN_KEY, t);
    else localStorage.removeItem(TOKEN_KEY);
  } catch { /* ignore */ }
}

export async function api(path, { method = 'GET', body } = {}) {
  const headers = { Accept: 'application/json' };
  const token = getToken();
  if (token) headers['X-Admin-Token'] = token;
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  const res = await fetch(BASE + path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = text; }
  if (!res.ok) {
    const detail = data && data.detail !== undefined ? data.detail : data;
    const err = new Error(`HTTP ${res.status}: ${typeof detail === 'string' ? detail : JSON.stringify(detail)}`);
    err.status = res.status;
    throw err;
  }
  return data;
}

// Polls `path` every `ms`. Each poll is independent: one failing endpoint (or a worker
// being offline) never blocks the others, and the last good data stays on screen.
export function usePoll(path, ms = 1000) {
  const [state, setState] = useState({ data: null, error: null, at: null });
  const busy = useRef(false);
  useEffect(() => {
    if (!path) return undefined;
    let alive = true;
    const tick = async () => {
      if (busy.current) return;
      busy.current = true;
      try {
        const data = await api(path);
        if (alive) setState({ data, error: null, at: Date.now() });
      } catch (e) {
        if (alive) setState((s) => ({ ...s, error: e.message }));
      } finally {
        busy.current = false;
      }
    };
    tick();
    const id = setInterval(tick, ms);
    return () => { alive = false; clearInterval(id); };
  }, [path, ms]);
  return state;
}

export const TERMINAL = new Set(['SUCCEEDED', 'FAILED', 'KILLED', 'CANCELLED', 'REJECTED']);

export function fmtTime(ts) {
  return ts ? new Date(ts * 1000).toLocaleTimeString() : '';
}

export function fmtNum(v, digits = 2) {
  return v === null || v === undefined ? '' : Number(v).toFixed(digits);
}

export function short(id) {
  return id ? id.slice(0, 8) : '';
}
