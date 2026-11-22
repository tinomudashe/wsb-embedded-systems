import React, { useCallback, useEffect, useRef, useState } from 'react';
import Icon from './components/Icon';
import WsbLogo from './components/WsbLogo';
import LoginView from './components/LoginView';
import MetricCards from './components/MetricCards';
import TrendChart from './components/TrendChart';
import InteractiveMap from './components/InteractiveMap';
import CalibrationPanel from './components/CalibrationPanel';
import RecentReadingsTable from './components/RecentReadingsTable';
import LogsPanel from './components/LogsPanel';
import RidesPanel from './components/RidesPanel';
import CydBoardEmulator from './components/CydBoardEmulator';
import ProfileModal from './components/ProfileModal';
import RegisterDeviceModal from './components/RegisterDeviceModal';
import { SelectField } from './components/FormControls';

const LS = typeof window !== 'undefined' ? window.localStorage : null;

function describeCommandToast(payload, deviceId) {
  const unit = deviceId || 'ESP32';
  switch (payload?.cmd) {
    case 'set_page': {
      const tabs = ['Environment', 'GPS Fix', 'QR & Wi-Fi', 'System Info'];
      const tabName = tabs[payload.page] || `Page ${payload.page}`;
      return `Switched ${unit} screen to the ${tabName} tab`;
    }
    case 'toast':
      return `Displayed banner on ${unit} screen: "${payload.message}"`;
    case 'led': {
      const modes = {
        auto: 'Auto Status',
        blue: 'Solid Blue',
        green: 'Solid Green',
        red: 'Solid Red',
        off: 'Off',
      };
      return `Set ${unit} RGB status LED to ${modes[payload.mode] || payload.mode}`;
    }
    case 'screen':
      if (payload.state === 'wake' || payload.state === 'active') {
        return `Woke up ${unit} screen (full brightness)`;
      }
      if (payload.state === 'dim') {
        return `Dimmed ${unit} screen to 10% power`;
      }
      if (payload.state === 'off') {
        return `Put ${unit} screen to sleep`;
      }
      return `Updated ${unit} screen power mode`;
    case 'sample_now':
      return `Requested immediate sensor reading & MQTT publish from ${unit}`;
    case 'invert':
      return `Flipped ${unit} display color polarity (saved to onboard memory)`;
    case 'start_ap':
      return `Opened ${unit} Wi-Fi Setup hotspot (WSB-CYD-Setup at 192.168.4.1)`;
    case 'stop_ap':
      return `Closed ${unit} Wi-Fi Setup hotspot and resumed normal Wi-Fi`;
    case 'reboot':
      return `Restarting ${unit} board — reconnecting in ~5 seconds`;
    default:
      return `Sent ${payload?.cmd || 'control'} update to ${unit}`;
  }
}

function getInitialQrToken() {
  if (typeof window === 'undefined') return null;
  const sp = new URLSearchParams(window.location.search);
  return sp.get('p') || sp.get('pair') || null;
}

export default function App() {
  const [authState, setAuthState] = useState('checking'); // 'checking' | 'guest' | 'authed'
  const [user, setUser] = useState('');
  const [profile, setProfile] = useState(null);
  const [pairToken, setPairToken] = useState(() => getInitialQrToken());

  const [devices, setDevices] = useState([]);
  const [deviceId, setDeviceId] = useState(() => LS?.getItem('wsb.device') || '');
  const [metric, setMetric] = useState(() => LS?.getItem('wsb.metric') || 'temp_c');
  const [hours, setHours] = useState(() => parseFloat(LS?.getItem('wsb.hours') || '1'));

  const [detail, setDetail] = useState(null);
  const [seriesPoints, setSeriesPoints] = useState([]);
  const [trackPoints, setTrackPoints] = useState([]);
  const [recent, setRecent] = useState([]);
  const [logs, setLogs] = useState([]);

  const [showProfileModal, setShowProfileModal] = useState(false);
  const [showRegisterModal, setShowRegisterModal] = useState(false);

  const [liveStatus, setLiveStatus] = useState({ mode: 'connecting', text: 'connecting…' });
  const [pulse, setPulse] = useState(false);
  const [utcClock, setUtcClock] = useState('');
  const [toast, setToast] = useState(null);

  const deviceIdRef = useRef(deviceId);
  deviceIdRef.current = deviceId;

  const showToast = useCallback((msg, kind = 'ok') => {
    setToast({ msg, kind });
  }, []);

  useEffect(() => {
    if (!toast) return;
    const t = setTimeout(() => setToast(null), 3500);
    return () => clearTimeout(t);
  }, [toast]);

  const api = useCallback(async (path, opts = {}) => {
    const r = await fetch(path, {
      credentials: 'same-origin',
      headers: opts.body ? { 'Content-Type': 'application/json' } : {},
      ...opts,
    });
    if (r.status === 401) {
      setAuthState('guest');
      throw new Error('unauthorized');
    }
    const body = r.headers.get('content-type')?.includes('json')
      ? await r.json()
      : await r.text();
    if (!r.ok) {
      throw new Error(typeof body?.detail === 'string' ? body.detail : `HTTP ${r.status}`);
    }
    return body;
  }, []);

  const refreshDevices = useCallback(
    async (preferredId) => {
      const list = await api('/api/devices');
      setDevices(list);
      if (list.length > 0) {
        const saved = preferredId || LS?.getItem('wsb.device');
        const chosen = list.some((d) => d.id === saved) ? saved : list[0].id;
        setDeviceId(chosen);
      }
      return list;
    },
    [api]
  );

  // Check session on startup
  useEffect(() => {
    fetch('/api/auth/me', { credentials: 'same-origin' })
      .then(async (r) => {
        if (!r.ok) {
          setAuthState('guest');
          return;
        }
        const data = await r.json();
        setUser(data.user);
        setProfile(data.profile || { username: data.user });
        setAuthState('authed');
      })
      .catch(() => setAuthState('guest'));
  }, []);

  // Load device list when authenticated (and auto-claim if opened via QR code)
  useEffect(() => {
    if (authState !== 'authed') return;
    (async () => {
      let preferred = null;
      if (pairToken) {
        try {
          const res = await api('/api/devices/claim', {
            method: 'POST',
            body: JSON.stringify({ code: pairToken }),
          });
          preferred = res.device?.id;
          showToast(`Linked ESP32 unit ${preferred} to your profile`);
        } catch {
          // ignore if token expired
        }
        setPairToken(null);
        window.history.replaceState({}, '', '/');
      }
      await refreshDevices(preferred);
    })().catch((e) => {
      if (e.message !== 'unauthorized') showToast(e.message, 'err');
    });
  }, [authState]);

  const loadDeviceDetail = useCallback(
    async (id) => {
      if (!id) return;
      const d = await api(`/api/devices/${encodeURIComponent(id)}`);
      setDetail(d);
    },
    [api]
  );

  const loadSeries = useCallback(
    async (id, h) => {
      if (!id) return;
      const pts = Math.min(600, Math.max(60, Math.round((h * 3600) / 10)));
      const res = await api(
        `/api/devices/${encodeURIComponent(id)}/series?hours=${h}&points=${pts}`
      );
      setSeriesPoints(res.points || []);
    },
    [api]
  );

  const loadTrack = useCallback(
    async (id, h) => {
      if (!id) return;
      const pts = await api(
        `/api/devices/${encodeURIComponent(id)}/track?hours=${Math.max(h, 24)}`
      );
      setTrackPoints(pts || []);
    },
    [api]
  );

  const loadRecent = useCallback(
    async (id) => {
      if (!id) return;
      const rows = await api(
        `/api/devices/${encodeURIComponent(id)}/telemetry?limit=25&order=desc&hours=720`
      );
      setRecent(rows || []);
    },
    [api]
  );

  const loadLogs = useCallback(
    async (id) => {
      const q = id ? `?device_id=${encodeURIComponent(id)}&limit=120` : '?limit=120';
      const rows = await api(`/api/logs${q}`);
      setLogs(rows || []);
    },
    [api]
  );

  // Load device data when deviceId changes
  useEffect(() => {
    if (authState !== 'authed' || !deviceId) return;
    LS?.setItem('wsb.device', deviceId);
    Promise.all([
      loadDeviceDetail(deviceId),
      loadSeries(deviceId, hours),
      loadTrack(deviceId, hours),
      loadRecent(deviceId),
      loadLogs(deviceId),
    ]).catch((e) => {
      if (e.message !== 'unauthorized') showToast(e.message, 'err');
    });
  }, [authState, deviceId]);

  useEffect(() => {
    if (authState !== 'authed' || !deviceId) return;
    LS?.setItem('wsb.hours', String(hours));
    loadSeries(deviceId, hours).catch(() => {});
    loadTrack(deviceId, hours).catch(() => {});
  }, [hours]);

  useEffect(() => {
    LS?.setItem('wsb.metric', metric);
  }, [metric]);

  // WebSocket live stream + automatic HTTP polling fallback
  useEffect(() => {
    if (authState !== 'authed') return;
    let ws = null;
    let pollTimer = null;
    let reconnectTimer = null;
    let backoff = 1000;
    let unmounted = false;

    function startPolling() {
      if (pollTimer || unmounted) return;
      pollTimer = setInterval(() => {
        const id = deviceIdRef.current;
        if (!id) return;
        Promise.all([loadDeviceDetail(id), loadRecent(id)]).catch(() => {
          setLiveStatus({ mode: 'off', text: 'offline' });
        });
      }, 5000);
    }

    function stopPolling() {
      if (pollTimer) {
        clearInterval(pollTimer);
        pollTimer = null;
      }
    }

    function handleEvent(ev) {
      const activeId = deviceIdRef.current;
      if (ev.type === 'telemetry' && ev.data?.device_id === activeId) {
        const t = ev.data;
        const tsSec = Math.floor(Date.parse(t.ts) / 1000);
        setDetail((prev) =>
          prev
            ? {
                ...prev,
                latest: t,
                last_fix: t.gps_fix
                  ? {
                      ts: t.ts,
                      lat: t.lat,
                      lon: t.lon,
                      alt: t.alt,
                      sats: t.sats,
                      hdop: t.hdop,
                    }
                  : prev.last_fix,
              }
            : prev
        );
        setSeriesPoints((prev) => [
          ...prev,
          {
            t: tsSec,
            temp_c: t.temp_c,
            humidity: t.humidity,
            pressure_hpa: t.pressure_hpa,
            aqi: t.aqi,
          },
        ]);
        if (t.gps_fix && t.lat !== null && t.lon !== null) {
          setTrackPoints((prev) => [
            ...prev,
            { ts: t.ts, lat: t.lat, lon: t.lon, alt: t.alt, speed_kmh: t.speed_kmh },
          ]);
        }
        setRecent((prev) => {
          const next = [t, ...prev];
          next.sort((a, b) => (a.ts < b.ts ? 1 : -1));
          return next.slice(0, 25);
        });
        setPulse(true);
        setTimeout(() => setPulse(false), 300);
      } else if (ev.type === 'status' && ev.device?.id === activeId) {
        setDetail((prev) => (prev ? { ...prev, device: ev.device } : prev));
      } else if (ev.type === 'config' && ev.device_id === activeId) {
        setDetail((prev) =>
          prev
            ? {
                ...prev,
                config: { ...ev.config, _updated_at: new Date().toISOString() },
              }
            : prev
        );
      } else if (ev.type === 'log' && ev.data) {
        setLogs((prev) => [ev.data, ...prev].slice(0, 150));
      }
    }

    function connect() {
      if (unmounted) return;
      const proto = window.location.protocol === 'https:' ? 'wss' : 'ws';
      ws = new WebSocket(`${proto}://${window.location.host}/ws`);
      setLiveStatus({ mode: 'connecting', text: 'connecting…' });

      ws.onopen = () => {
        if (unmounted) return;
        backoff = 1000;
        setLiveStatus({ mode: 'on', text: 'live stream' });
        stopPolling();
      };

      ws.onmessage = (m) => {
        try {
          handleEvent(JSON.parse(m.data));
        } catch {
          // ignore
        }
      };

      ws.onclose = (e) => {
        if (unmounted) return;
        if (e.code === 4401) {
          setAuthState('guest');
          return;
        }
        setLiveStatus({ mode: 'poll', text: 'polling (5s)' });
        startPolling();
        reconnectTimer = setTimeout(connect, backoff);
        backoff = Math.min(backoff * 2, 30000);
      };
    }

    connect();
    const statsRefresh = setInterval(() => {
      if (deviceIdRef.current) loadDeviceDetail(deviceIdRef.current).catch(() => {});
    }, 60000);

    return () => {
      unmounted = true;
      stopPolling();
      clearTimeout(reconnectTimer);
      clearInterval(statsRefresh);
      if (ws) ws.close();
    };
  }, [authState, loadDeviceDetail, loadRecent]);

  // 1-second UTC clock
  useEffect(() => {
    const updateClock = () => {
      setUtcClock(`${new Date().toISOString().slice(11, 19)} UTC`);
    };
    updateClock();
    const id = setInterval(updateClock, 1000);
    return () => clearInterval(id);
  }, []);

  async function handleSaveConfig(payload) {
    const saved = await api(`/api/devices/${encodeURIComponent(deviceId)}/config`, {
      method: 'PUT',
      body: JSON.stringify(payload),
    });
    setDetail((prev) =>
      prev
        ? {
            ...prev,
            config: { ...saved, _updated_at: new Date().toISOString(), _updated_by: user },
          }
        : prev
    );
    showToast(
      saved.pushed
        ? `Saved calibration v${saved.version} and pushed live to ${deviceId}`
        : `Saved calibration v${saved.version} for ${deviceId} (queued for next connection)`,
      'ok'
    );
    return saved;
  }

  async function handleSaveProfile(payload) {
    const updated = await api('/api/auth/profile', {
      method: 'PUT',
      body: JSON.stringify(payload),
    });
    setProfile(updated);
    showToast('Saved your user profile details');
  }

  async function handleChangePassword(payload) {
    await api('/api/auth/password', {
      method: 'POST',
      body: JSON.stringify(payload),
    });
    showToast('Updated your account password');
  }

  async function handleRegisterDevice(payload) {
    const created = await api('/api/devices', {
      method: 'POST',
      body: JSON.stringify(payload),
    });
    await refreshDevices(created.device?.id);
    showToast(`Registered new ESP32 unit ${created.device?.id}`);
    return created;
  }

  async function handleClaimDevice(payload) {
    const res = await api('/api/devices/claim', {
      method: 'POST',
      body: JSON.stringify(payload),
    });
    await refreshDevices(res.device?.id);
    showToast(`Linked ESP32 unit ${res.device?.id} to your profile`);
    return res;
  }

  async function handleLogout() {
    await fetch('/api/auth/logout', { method: 'POST', credentials: 'same-origin' });
    setAuthState('guest');
    setUser('');
    setProfile(null);
    showToast('Signed out of WSB Merito Telemetry Hub');
  }

  async function handleClearLogs() {
    await api(`/api/logs?device_id=${encodeURIComponent(deviceId)}`, { method: 'DELETE' });
    await loadLogs(deviceId);
    showToast(`Cleared diagnostic logs for ${deviceId || 'all devices'}`);
  }

  async function handleSendCommand(payload, customToast) {
    const res = await api(`/api/devices/${encodeURIComponent(deviceId)}/command`, {
      method: 'POST',
      body: JSON.stringify(payload),
    });
    if (customToast !== null) {
      showToast(customToast || describeCommandToast(payload, deviceId));
    }
    return res;
  }

  async function handleSimulatePacket(payload) {
    const res = await api(`/api/devices/${encodeURIComponent(deviceId)}/simulate`, {
      method: 'POST',
      body: JSON.stringify(payload),
    });
    const t = Number(payload.temp_c).toFixed(1);
    const h = Number(payload.humidity).toFixed(1);
    const p = Number(payload.pressure_hpa).toFixed(1);
    showToast(`Published simulated reading (${t} °C, ${h} %RH, ${p} hPa) for ${deviceId}`);
    return res;
  }

  if (authState === 'checking') {
    return (
      <div className="login-body">
        <div className="muted small">Loading telemetry session…</div>
      </div>
    );
  }

  if (authState === 'guest') {
    return (
      <>
        <LoginView
          pairToken={pairToken}
          onLoginSuccess={(u, prof, claimedId) => {
            setUser(u);
            setProfile(prof || { username: u });
            setPairToken(null);
            if (claimedId) {
              LS?.setItem('wsb.device', claimedId);
              setDeviceId(claimedId);
            }
            setAuthState('authed');
            window.history.replaceState({}, '', '/');
            showToast(
              claimedId
                ? `Signed in as ${prof?.full_name || u} · paired ${claimedId}`
                : `Signed in successfully as ${prof?.full_name || u}`,
              'ok'
            );
          }}
        />
        {toast && (
          <div className={`toast toast-${toast.kind}`} role="status">
            <Icon name="check-circle" />
            <span>{toast.msg}</span>
          </div>
        )}
      </>
    );
  }

  const device = detail?.device;

  return (
    <>
      <header className="topbar">
        <WsbLogo size="md" />

        <div className="topbar-mid">
          <SelectField
            icon="microchip"
            ariaLabel="Select ESP32 device"
            value={deviceId}
            options={devices.map((d) => ({
              value: d.id,
              label: d.name && d.name !== d.id ? `${d.name} (${d.id})` : d.id,
            }))}
            onChange={(val) => setDeviceId(val)}
          />
          <span className={`badge ${device?.online ? 'badge-on' : 'badge-off'}`}>
            <Icon name={device?.online ? 'check-circle' : 'circle-xmark'} />
            <span>{device?.online ? 'online' : 'offline'}</span>
          </span>
          <button
            type="button"
            className="btn"
            onClick={() => setShowRegisterModal(true)}
          >
            Register Device
          </button>
        </div>

        <div className="topbar-right">
          <span
            className={`live live-${liveStatus.mode} ${pulse ? 'pulse' : ''}`}
            title="WebSocket connection status"
          >
            <i className="live-dot" />
            <span>{liveStatus.text}</span>
          </span>

          <span className="mono muted small topbar-clock">
            <Icon name="clock" /> {utcClock}
          </span>

          <button
            type="button"
            className="btn"
            onClick={() => setShowProfileModal(true)}
          >
            Profile ({profile?.full_name || user})
          </button>

          <button type="button" className="btn" onClick={handleLogout}>
            Sign out
          </button>
        </div>
      </header>

      <main className="container">
        <MetricCards
          latest={detail?.latest}
          stats={detail?.stats_24h}
          device={device}
          activeMetric={metric}
          onSelectMetric={setMetric}
        />

        <section className="grid-2">
          <TrendChart
            metric={metric}
            hours={hours}
            points={seriesPoints}
            onMetricChange={setMetric}
            onHoursChange={setHours}
          />

          <InteractiveMap
            latest={detail?.latest}
            lastFix={detail?.last_fix}
            track={trackPoints}
            deviceId={deviceId}
          />
        </section>

        <CydBoardEmulator
          deviceId={deviceId}
          device={device}
          pairing={detail?.pairing}
          latest={detail?.latest}
          seriesPoints={seriesPoints}
          config={detail?.config}
          onSendCommand={handleSendCommand}
          onSimulatePacket={handleSimulatePacket}
          onNotify={showToast}
        />

        <CalibrationPanel
          device={device}
          latest={detail?.latest}
          stats={detail?.stats_24h}
          mqtt={detail?.mqtt}
          config={detail?.config}
          onSaveConfig={handleSaveConfig}
        />

        <RidesPanel deviceId={deviceId} api={api} onNotify={showToast} />

        <RecentReadingsTable deviceId={deviceId} recent={recent} />

        <LogsPanel
          logs={logs}
          deviceId={deviceId}
          onRefresh={() => loadLogs(deviceId)}
          onClear={handleClearLogs}
        />
      </main>

      {showProfileModal && (
        <ProfileModal
          profile={profile || { username: user }}
          onClose={() => setShowProfileModal(false)}
          onSaveProfile={handleSaveProfile}
          onChangePassword={handleChangePassword}
        />
      )}

      {showRegisterModal && (
        <RegisterDeviceModal
          onClose={() => setShowRegisterModal(false)}
          onRegisterDevice={handleRegisterDevice}
          onClaimDevice={handleClaimDevice}
        />
      )}

      {toast && (
        <div className={`toast toast-${toast.kind}`} role="status">
          <Icon
            name={
              toast.kind === 'err'
                ? 'triangle-warning'
                : toast.kind === 'warn'
                  ? 'exclamation'
                  : 'check-circle'
            }
          />
          <span>{toast.msg}</span>
        </div>
      )}
    </>
  );
}
