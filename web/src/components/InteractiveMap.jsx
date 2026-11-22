import React, { useEffect, useMemo, useRef, useState } from 'react';
import * as maplibregl from 'maplibre-gl';
// MapLibre v6 derives its worker URL from import.meta.url, which points at the
// bundled chunk after a Vite build. Emit the worker as an asset and point at it.
import maplibreWorkerUrl from 'maplibre-gl/dist/maplibre-gl-worker.mjs?url';
import 'maplibre-gl/dist/maplibre-gl.css';
import Icon from './Icon';
import { fmt } from './MetricCards';

maplibregl.setWorkerUrl(maplibreWorkerUrl);

const SATELLITE_STYLE = {
  version: 8,
  sources: {
    esri: {
      type: 'raster',
      tiles: [
        'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
      ],
      tileSize: 256,
      attribution: 'Tiles &copy; Esri',
    },
  },
  layers: [{ id: 'esri-sat', type: 'raster', source: 'esri' }],
};

/** Generate a 64-vertex GeoJSON polygon circle around [lon, lat] with radiusMeters */
function createGeoJSONCircle(lon, lat, radiusMeters, points = 64) {
  if (lon === null || lat === null || lon === undefined || lat === undefined) {
    return { type: 'FeatureCollection', features: [] };
  }
  const km = radiusMeters / 1000;
  const ret = [];
  const distanceX = km / (111.32 * Math.cos((lat * Math.PI) / 180));
  const distanceY = km / 110.574;
  for (let i = 0; i < points; i++) {
    const theta = (i / points) * (2 * Math.PI);
    const x = distanceX * Math.cos(theta);
    const y = distanceY * Math.sin(theta);
    ret.push([lon + x, lat + y]);
  }
  ret.push(ret[0]);
  return {
    type: 'FeatureCollection',
    features: [
      {
        type: 'Feature',
        geometry: { type: 'Polygon', coordinates: [ret] },
      },
    ],
  };
}

function addOverlayLayers(map, trackGeoJSON, circleGeoJSON) {
  if (!map.getSource('wsb-track')) {
    map.addSource('wsb-track', { type: 'geojson', data: trackGeoJSON });
    map.addLayer({
      id: 'wsb-track-line',
      type: 'line',
      source: 'wsb-track',
      layout: { 'line-join': 'round', 'line-cap': 'round' },
      paint: { 'line-color': '#0066ff', 'line-width': 3.5, 'line-opacity': 0.95 },
    });
  } else {
    map.getSource('wsb-track').setData(trackGeoJSON);
  }

  if (!map.getSource('wsb-accuracy')) {
    map.addSource('wsb-accuracy', { type: 'geojson', data: circleGeoJSON });
    map.addLayer({
      id: 'wsb-accuracy-fill',
      type: 'fill',
      source: 'wsb-accuracy',
      paint: { 'fill-color': '#0066ff', 'fill-opacity': 0.14 },
    });
    map.addLayer({
      id: 'wsb-accuracy-outline',
      type: 'line',
      source: 'wsb-accuracy',
      paint: { 'line-color': '#0066ff', 'line-width': 1.5, 'line-dasharray': [2, 2] },
    });
  } else {
    map.getSource('wsb-accuracy').setData(circleGeoJSON);
  }
}

/**
 * 21st.dev Interactive GPS Map / Mapcn component powered by MapLibre GL (Satellite only).
 */
export default function InteractiveMap({ latest, lastFix, track, deviceId }) {
  const containerRef = useRef(null);
  const mapRef = useRef(null);
  const markerRef = useRef(null);
  const hasCenteredRef = useRef(false);

  const [follow, setFollow] = useState(true);
  const [is3D, setIs3D] = useState(false);
  const [expanded, setExpanded] = useState(false);
  const [showPopup, setShowPopup] = useState(true);

  const fix = useMemo(() => {
    if (latest?.gps_fix && latest.lat !== null && latest.lon !== null) {
      return {
        lat: latest.lat,
        lon: latest.lon,
        alt: latest.alt,
        speed_kmh: latest.speed_kmh,
        course: latest.course,
        sats: latest.sats,
        hdop: latest.hdop,
        ts: latest.ts,
        live: true,
      };
    }
    if (lastFix && lastFix.lat !== null && lastFix.lon !== null) {
      return { ...lastFix, live: false };
    }
    if (track?.length) {
      const last = track[track.length - 1];
      return { ...last, live: false };
    }
    return null;
  }, [latest, lastFix, track]);

  const trackGeoJSON = useMemo(
    () => ({
      type: 'FeatureCollection',
      features:
        track && track.length > 1
          ? [
              {
                type: 'Feature',
                geometry: {
                  type: 'LineString',
                  coordinates: track.map((p) => [p.lon, p.lat]),
                },
              },
            ]
          : [],
    }),
    [track]
  );

  const circleGeoJSON = useMemo(() => {
    if (!fix) return { type: 'FeatureCollection', features: [] };
    const radiusMeters = Math.max(6, (fix.hdop || 1.5) * 5);
    return createGeoJSONCircle(fix.lon, fix.lat, radiusMeters);
  }, [fix]);

  useEffect(() => {
    if (!containerRef.current || mapRef.current) return;

    const map = new maplibregl.Map({
      container: containerRef.current,
      style: SATELLITE_STYLE,
      center: [21.0122, 52.2297],
      zoom: 5,
      pitch: 0,
      attributionControl: false,
    });
    map.addControl(new maplibregl.AttributionControl({ compact: true }), 'bottom-right');

    const el = document.createElement('button');
    el.type = 'button';
    el.className = 'mapcn-pin';
    el.setAttribute('aria-label', 'ESP32 GPS Marker');
    el.innerHTML = `<span class="mapcn-pin-core"></span>`;
    el.addEventListener('click', (e) => {
      e.stopPropagation();
      setShowPopup((v) => !v);
    });

    const marker = new maplibregl.Marker({ element: el, anchor: 'center' });
    markerRef.current = marker;
    mapRef.current = map;

    map.on('load', () => {
      addOverlayLayers(map, trackGeoJSON, circleGeoJSON);
    });

    return () => {
      marker.remove();
      map.remove();
      mapRef.current = null;
    };
  }, []);

  useEffect(() => {
    hasCenteredRef.current = false;
  }, [deviceId]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !map.isStyleLoaded()) return;
    addOverlayLayers(map, trackGeoJSON, circleGeoJSON);
  }, [trackGeoJSON, circleGeoJSON]);

  useEffect(() => {
    const map = mapRef.current;
    const marker = markerRef.current;
    if (!map || !marker) return;

    if (!fix) {
      marker.remove();
      return;
    }

    const lngLat = [fix.lon, fix.lat];
    marker.setLngLat(lngLat).addTo(map);

    if (!hasCenteredRef.current) {
      hasCenteredRef.current = true;
      map.jumpTo({ center: lngLat, zoom: 15.5 });
    } else if (follow) {
      map.easeTo({ center: lngLat, duration: 650 });
    }
  }, [fix, follow]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map) return;
    const t = setTimeout(() => map.resize(), 60);
    return () => clearTimeout(t);
  }, [expanded]);

  function handleRecenter() {
    if (!mapRef.current || !fix) return;
    mapRef.current.flyTo({
      center: [fix.lon, fix.lat],
      zoom: Math.max(mapRef.current.getZoom(), 15.5),
      duration: 700,
    });
  }

  function handleToggle3D() {
    const next = !is3D;
    setIs3D(next);
    if (mapRef.current) {
      mapRef.current.easeTo({ pitch: next ? 52 : 0, bearing: next ? -15 : 0, duration: 500 });
    }
  }

  function handleZoom(delta) {
    if (!mapRef.current) return;
    mapRef.current.zoomTo(mapRef.current.getZoom() + delta, { duration: 250 });
  }

  return (
    <article className={`card panel mapcn-panel ${expanded ? 'mapcn-expanded' : ''}`}>
      <header className="panel-head">
        <h2>
          <Icon name="map-marker-track" /> GPS Location &amp; Trajectory
        </h2>

        <span className="mono muted small mapcn-coords-pill">
          <Icon name="marker" />
          {fix ? `${fix.lat.toFixed(6)}, ${fix.lon.toFixed(6)}` : 'Waiting for GPS fix'}
        </span>

        <span className="mapcn-sat-badge">
          <Icon name="globe" />
          <span>Satellite</span>
        </span>
      </header>

      <div className="mapcn-stage">
        <div ref={containerRef} className="map-box" />

        <div className="mapcn-controls">
          <button
            type="button"
            className={`mapcn-ctrl-btn ${follow ? 'active' : ''}`}
            onClick={() => setFollow((v) => !v)}
            title="Auto-follow live GPS updates"
          >
            Follow
          </button>
          <button
            type="button"
            className="mapcn-ctrl-btn"
            onClick={handleRecenter}
            disabled={!fix}
            title="Center on current GPS fix"
          >
            Center
          </button>
          <button
            type="button"
            className={`mapcn-ctrl-btn ${is3D ? 'active' : ''}`}
            onClick={handleToggle3D}
            title="Toggle 3D perspective pitch"
          >
            3D
          </button>
          <button
            type="button"
            className="mapcn-ctrl-btn"
            onClick={() => handleZoom(1)}
            title="Zoom in"
          >
            +
          </button>
          <button
            type="button"
            className="mapcn-ctrl-btn"
            onClick={() => handleZoom(-1)}
            title="Zoom out"
          >
            -
          </button>
          <button
            type="button"
            className="mapcn-ctrl-btn"
            onClick={() => setExpanded((v) => !v)}
            title={expanded ? 'Exit full screen' : 'Expand map'}
          >
            {expanded ? 'Collapse' : 'Expand'}
          </button>
        </div>

        {fix && showPopup && (
          <div className="mapcn-popup" role="dialog" aria-label="Telemetry marker details">
            <div className="mapcn-popup-head">
              <span className={`mapcn-status-dot ${fix.live ? 'live' : 'cached'}`} />
              <strong>{deviceId}</strong>
              <span className="muted small">
                {fix.live ? 'Live 3D Fix' : 'Last Known Fix'}
              </span>
              <button
                type="button"
                className="mapcn-popup-close"
                onClick={() => setShowPopup(false)}
                aria-label="Close popup"
              >
                Close
              </button>
            </div>
            <div className="mapcn-popup-grid">
              <div>
                <span className="muted">Coordinates</span>
                <strong className="mono">
                  {fix.lat.toFixed(5)}, {fix.lon.toFixed(5)}
                </strong>
              </div>
              <div>
                <span className="muted">Altitude</span>
                <strong className="mono">{fmt(fix.alt, 1)} m</strong>
              </div>
              <div>
                <span className="muted">Speed</span>
                <strong className="mono">{fmt(fix.speed_kmh, 1)} km/h</strong>
              </div>
              <div>
                <span className="muted">Satellites / HDOP</span>
                <strong className="mono">
                  {fix.sats ?? '--'} / {fmt(fix.hdop, 1)}
                </strong>
              </div>
            </div>
            <div className="mapcn-popup-foot muted small">
              <Icon name="clock" />
              <span>{fix.ts ? new Date(fix.ts).toLocaleString() : '--'}</span>
              <span className="spacer" />
              <span>{track?.length || 0} pts</span>
            </div>
          </div>
        )}
      </div>
    </article>
  );
}
