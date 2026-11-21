// Small inline line icons (no external resources: the demo may be offline).

const PATHS = {
  laptop: 'M4 5h16v10H4z M2 19h20',
  server: 'M4 4h16v6H4z M4 14h16v6H4z M8 7h.01 M8 17h.01',
  queue: 'M4 6h16 M4 12h16 M4 18h10',
  cpu: 'M7 7h10v10H7z M10 3v4 M14 3v4 M10 17v4 M14 17v4 M3 10h4 M3 14h4 M17 10h4 M17 14h4',
  ram: 'M3 8h18v8H3z M7 8v8 M11 8v8 M15 8v8',
  clock: 'M12 3a9 9 0 1 0 0 18a9 9 0 1 0 0-18z M12 7v5l3 2',
  check: 'M5 12l5 5l9-10',
  cross: 'M6 6l12 12 M18 6L6 18',
  arrow: 'M4 12h14 M13 6l6 6l-6 6',
  pick: 'M12 4v12 M6 10l6 6l6-6 M4 20h16',
  wifiOff: 'M2 2l20 20 M8.5 16.5a5 5 0 0 1 7 0 M5 12.5a10 10 0 0 1 5-2.5 M12 20h.01',
  wifi: 'M2 8.5a15 15 0 0 1 20 0 M5 12.5a10 10 0 0 1 14 0 M8.5 16.5a5 5 0 0 1 7 0 M12 20h.01',
  flame: 'M12 3c1 4 5 5 5 10a5 5 0 0 1-10 0c0-3 2-4 2-6c1 1 2 2 3 2c0-2-1-4 0-6z',
  info: 'M12 3a9 9 0 1 0 0 18a9 9 0 1 0 0-18z M12 11v5 M12 8h.01',
  play: 'M7 5l12 7l-12 7z',
  stop: 'M6 6h12v12H6z',
};

export default function Icon({ name, size = 14, className = '' }) {
  return (
    <svg className={`icon ${className}`} width={size} height={size} viewBox="0 0 24 24" fill="none"
         stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d={PATHS[name] || PATHS.info} />
    </svg>
  );
}
