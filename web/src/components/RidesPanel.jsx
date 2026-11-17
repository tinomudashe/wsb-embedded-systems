import React, { useCallback, useEffect, useMemo, useState } from 'react';
import Icon from './Icon';
import { fmt } from './MetricCards';

function formatDuration(seconds) {
  if (seconds === null || seconds === undefined || Number.isNaN(Number(seconds))) return '--';
  const total = Math.max(0, Math.round(Number(seconds)));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  if (h > 0) return `${h}h ${String(m).padStart(2, '0')}m`;
  if (m > 0) return `${m}m ${String(s).padStart(2, '0')}s`;
  return `${s}s`;
}

function formatDistance(meters) {
  if (meters === null || meters === undefined || Number.isNaN(Number(meters))) return '--';
  const m = Number(meters);
  return m >= 1000 ? `${(m / 1000).toFixed(2)} km` : `${Math.round(m)} m`;
}

function formatDate(iso) {
  if (!iso) return '--';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString(undefined, {
    year: 'numeric',
    month: 'short',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  });
}

function metric(stats, field, aggregate = 'avg', digits = 1) {
  const v = stats?.[field]?.[aggregate];
  return v === null || v === undefined ? '--' : fmt(v, digits);
}

/** Lightweight SVG preview of a recorded GPS track (no map tiles / extra deps). */
function TrackPreview({ track }) {
  const points = useMemo(
    () => (track || []).filter((p) => p && p.lat !== null && p.lon !== null),
    [track]
  );

  if (points.length === 0) {
    return (
      <div className="ride-track-empty muted small">
        <Icon name="map-marker-track" />
        <span>No GPS fixes in this ride</span>
      </div>
    );
  }

  const W = 320;
  const H = 180;
  const PAD = 18;
  const lats = points.map((p) => p.lat);
  const lons = points.map((p) => p.lon);
  let minLat = Math.min(...lats);
  let maxLat = Math.max(...lats);
  let minLon = Math.min(...lons);
  let maxLon = Math.max(...lons);
  if (maxLat - minLat < 1e-5) {
    minLat -= 0.00005;
    maxLat += 0.00005;
  }
  if (maxLon - minLon < 1e-5) {
    minLon -= 0.00005;
    maxLon += 0.00005;
  }

  const project = (p) => {
    const x = PAD + ((p.lon - minLon) / (maxLon - minLon)) * (W - PAD * 2);
    const y = H - PAD - ((p.lat - minLat) / (maxLat - minLat)) * (H - PAD * 2);
    return [Math.round(x * 10) / 10, Math.round(y * 10) / 10];
  };

  const xy = points.map(project);
  const path = xy.map(([x, y], i) => `${i === 0 ? 'M' : 'L'}${x},${y}`).join(' ');
  const [sx, sy] = xy[0];
  const [ex, ey] = xy[xy.length - 1];

  return (
    <svg
      className="ride-track"
      viewBox={`0 0 ${W} ${H}`}
      role="img"
      aria-label={`Recorded track with ${points.length} GPS fixes`}
    >
      <rect x="0" y="0" width={W} height={H} rx="10" className="ride-track-bg" />
      {[0.25, 0.5, 0.75].map((f) => (
        <g key={f}>
          <line x1={PAD} x2={W - PAD} y1={H * f} y2={H * f} className="ride-track-grid" />
          <line x1={W * f} x2={W * f} y1={PAD} y2={H - PAD} className="ride-track-grid" />
        </g>
      ))}
      {xy.length > 1 && <path d={path} className="ride-track-line" />}
      <circle cx={sx} cy={sy} r="5" className="ride-track-start" />
      <circle cx={ex} cy={ey} r="4" className="ride-track-end" />
    </svg>
  );
}

export default function RidesPanel({ deviceId, api, onNotify }) {
  const [rides, setRides] = useState([]);
  const [selectedId, setSelectedId] = useState(null);
  const [detail, setDetail] = useState(null);
  const [loading, setLoading] = useState(true);
  const [detailLoading, setDetailLoading] = useState(false);
  const [error, setError] = useState('');

  const loadRides = useCallback(
    async (preferredId) => {
      if (!deviceId) return [];
      const rows = await api(`/api/devices/${encodeURIComponent(deviceId)}/rides?limit=50`);
      const list = Array.isArray(rows) ? rows : [];
      setRides(list);
      setSelectedId((prev) => {
        const wanted = preferredId || prev;
        return list.some((r) => r.id === wanted) ? wanted : list[0]?.id ?? null;
      });
      return list;
    },
    [api, deviceId]
  );

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError('');
    setRides([]);
    setDetail(null);
    setSelectedId(null);
    loadRides()
      .catch((e) => {
        if (!cancelled && e.message !== 'unauthorized') setError(e.message);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [loadRides]);

  useEffect(() => {
    let cancelled = false;
    if (!selectedId) {
      setDetail(null);
      return undefined;
    }
    setDetailLoading(true);
    api(`/api/rides/${encodeURIComponent(selectedId)}`)
      .then((data) => {
        if (!cancelled) setDetail(data);
      })
      .catch((e) => {
        if (!cancelled && e.message !== 'unauthorized') setError(e.message);
      })
      .finally(() => {
        if (!cancelled) setDetailLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [api, selectedId]);

  async function handleRefresh() {
    setError('');
    setLoading(true);
    try {
      await loadRides();
      onNotify?.('Ride list refreshed');
    } catch (e) {
      if (e.message !== 'unauthorized') setError(e.message);
    } finally {
      setLoading(false);
    }
  }

  const stats = detail?.stats || {};
  const selectedSummary = rides.find((r) => r.id === selectedId) || null;

  return (
    <article className="card panel rides-panel">
      <header className="panel-head">
        <h2>
          <Icon name="map-marker-track" /> Rides &amp; Recorded Tracks
        </h2>
        <span className="badge">{rides.length} ride{rides.length === 1 ? '' : 's'}</span>
        <button type="button" className="btn" onClick={handleRefresh} disabled={loading}>
          <Icon name="rotate-right" /> Refresh
        </button>
        {detail && (
          <a className="btn" href={`/api/rides/${encodeURIComponent(detail.id)}/export.csv`} download>
            Download CSV
          </a>
        )}
      </header>

      {error && (
        <div className="form-error" role="alert">
          <Icon name="triangle-warning" /> <span>{error}</span>
        </div>
      )}

      {loading && rides.length === 0 ? (
        <div className="rides-empty muted">
          <span className="spinner-dot" /> Loading recorded rides…
        </div>
      ) : rides.length === 0 ? (
        <div className="rides-empty">
          <Icon name="map-marker-track" />
          <strong>No rides recorded yet</strong>
          <p className="muted small">
            Insert a microSD card and tap <b>Start REC</b> on the ESP32 System tab. Once the device
            uploads the CSV, the ride, its track and PM statistics appear here.
          </p>
        </div>
      ) : (
        <div className="rides-layout">
          <div className="ride-list" role="list" aria-label="Recorded rides">
            {rides.map((ride) => {
              const active = ride.id === selectedId;
              return (
                <button
                  type="button"
                  key={ride.id}
                  role="listitem"
                  className={`ride-item ${active ? 'active' : ''}`}
                  onClick={() => setSelectedId(ride.id)}
                  aria-pressed={active}
                >
                  <div className="ride-item-head">
                    <span className="ride-item-title">
                      <strong>{formatDate(ride.started_at)}</strong>
                      {active && <span className="badge">Selected</span>}
                    </span>
                    <span className="muted small mono ride-item-file">{ride.filename}</span>
                  </div>
                  <div className="ride-item-metrics">
                    <span>
                      <small>Duration</small>
                      <strong>{formatDuration(ride.duration_s)}</strong>
                    </span>
                    <span>
                      <small>Distance</small>
                      <strong>{formatDistance(ride.distance_m)}</strong>
                    </span>
                    <span>
                      <small>Samples</small>
                      <strong>{ride.sample_count ?? '--'}</strong>
                    </span>
                    <span>
                      <small>PM2.5 avg / max</small>
                      <strong>
                        {metric(ride.stats, 'pm2_5', 'avg')} / {metric(ride.stats, 'pm2_5', 'max')}
                      </strong>
                    </span>
                  </div>
                </button>
              );
            })}
          </div>

          <div className="ride-detail">
            {detailLoading || !detail ? (
              <div className="rides-empty muted">
                <span className="spinner-dot" /> Loading ride track…
              </div>
            ) : (
              <>
                <div className="ride-detail-head">
                  <div>
                    <h3>{selectedSummary?.filename || detail.filename}</h3>
                    <p className="muted small">
                      {formatDate(detail.started_at)} → {formatDate(detail.ended_at)} ·{' '}
                      {detail.device_id}
                    </p>
                  </div>
                  <a
                    className="btn"
                    href={`/api/rides/${encodeURIComponent(detail.id)}/export.csv`}
                    download
                  >
                    Export CSV
                  </a>
                </div>

                <TrackPreview track={detail.track} />

                <div className="ride-stats-grid">
                  <div>
                    <span className="muted small">Distance</span>
                    <strong>{formatDistance(detail.distance_m)}</strong>
                  </div>
                  <div>
                    <span className="muted small">Duration</span>
                    <strong>{formatDuration(detail.duration_s)}</strong>
                  </div>
                  <div>
                    <span className="muted small">Samples / fixes</span>
                    <strong>
                      {detail.sample_count ?? '--'} / {stats.fixes ?? '--'}
                    </strong>
                  </div>
                  <div>
                    <span className="muted small">PM2.5 avg</span>
                    <strong>{metric(stats, 'pm2_5', 'avg')} µg/m³</strong>
                  </div>
                  <div>
                    <span className="muted small">PM2.5 max</span>
                    <strong>{metric(stats, 'pm2_5', 'max')} µg/m³</strong>
                  </div>
                  <div>
                    <span className="muted small">PM10 avg / max</span>
                    <strong>
                      {metric(stats, 'pm10', 'avg')} / {metric(stats, 'pm10', 'max')} µg/m³
                    </strong>
                  </div>
                  <div>
                    <span className="muted small">Temp avg</span>
                    <strong>{metric(stats, 'temp_c', 'avg')} °C</strong>
                  </div>
                  <div>
                    <span className="muted small">Humidity avg</span>
                    <strong>{metric(stats, 'humidity', 'avg')} %</strong>
                  </div>
                  <div>
                    <span className="muted small">Pressure avg</span>
                    <strong>{metric(stats, 'pressure_hpa', 'avg')} hPa</strong>
                  </div>
                  <div>
                    <span className="muted small">AQI avg</span>
                    <strong>{metric(stats, 'aqi', 'avg', 0)}</strong>
                  </div>
                </div>

                <p className="muted small ride-detail-foot">
                  <Icon name="clock" /> Uploaded {formatDate(detail.uploaded_at)} ·{' '}
                  {detail.track?.length || 0} track points
                  {detail.config?.version ? ` · config v${detail.config.version}` : ''}
                </p>
              </>
            )}
          </div>
        </div>
      )}
    </article>
  );
}
