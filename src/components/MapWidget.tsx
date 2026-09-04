import { useEffect, useRef, useState } from "react";
import maplibregl from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import { Viewer } from "mapillary-js";
import "mapillary-js/dist/mapillary.css";

export interface MapTarget {
  lat: number;
  lon: number;
  name: string;
}

interface MapWidgetProps {
  /** Set by "Jarvis, pull up a map of ..." (tools.py's show_map, relayed
   * over the desktop WebSocket as a 'show_map' message) -- null until the
   * first request, so the widget starts on a sensible default rather than
   * an arbitrary empty map. */
  target: MapTarget | null;
  /** Set (independently of target) by "Jarvis, show me the radar over
   * ..." (show_weather_radar) -- toggles radar mode and optionally
   * re-centers at the same time. Bumped by a counter, not a boolean,
   * so asking for radar on the same city twice still visibly reacts. */
  radarRequest: { target: MapTarget | null; nonce: number } | null;
}

// Bethel, CT -- same default the Weather widget already centers on
// (see WeatherWidget.tsx), so a freshly-opened map widget lines up with
// what the weather widget is already showing instead of some unrelated
// arbitrary spot.
const DEFAULT_TARGET: MapTarget = { lat: 41.3712, lon: -73.4140, name: "Bethel, CT" };

const MAPTILER_KEY = import.meta.env.VITE_MAPTILER_KEY as string | undefined;
// Real vector dark style either way (crisp glowing roads/labels from
// actual vector data, not a CSS filter over a raster photo tile) -- if a
// MapTiler key is set (see .env.example), use that; otherwise
// OpenFreeMap's hosted "dark" style, which needs no key/account/signup at
// all (genuinely free vector tiles: https://openfreemap.org) and is the
// default until/unless a MapTiler key is added. Both are real dark vector
// styles, so there's no degraded fallback tier anymore -- just two
// providers for the same look.
const STYLE_URL = MAPTILER_KEY
  ? `https://api.maptiler.com/maps/dataviz-dark/style.json?key=${MAPTILER_KEY}`
  : "https://tiles.openfreemap.org/styles/dark";

// Free Mapillary client token (see .env.example) -- unlike Google Street
// View, no billing/card required, just a free account. Genuinely optional:
// the STREET VIEW button still appears without one, it just explains what
// to do instead of failing silently.
const MAPILLARY_TOKEN = import.meta.env.VITE_MAPILLARY_TOKEN as string | undefined;

// Nominatim's display_name is a full address hierarchy (street, neighborhood,
// borough, county, state, zip, country) -- great in the search dropdown,
// too long for the small HUD location chip. First two comma-separated
// parts read as a real short place name in both cases (voice-command
// targets and search results alike, since voice targets are usually
// already short like "Bethel, CT" -- slicing a 2-part name is a no-op).
function shortName(name: string): string {
  return name.split(",").slice(0, 2).join(",").trim();
}

function buildMarkerEl(): HTMLDivElement {
  const el = document.createElement("div");
  el.className = "map-widget__marker";
  el.innerHTML = '<span class="map-widget__marker-ping"></span><span class="map-widget__marker-dot"></span>';
  return el;
}

export function MapWidget({ target, radarRequest }: MapWidgetProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const mapRef = useRef<maplibregl.Map | null>(null);
  const markerRef = useRef<maplibregl.Marker | null>(null);
  const radarTimerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const [radarOn, setRadarOn] = useState(false);
  const [radarLoading, setRadarLoading] = useState(false);
  const [mapReady, setMapReady] = useState(false);

  // Wherever the marker/view actually is right now -- updated by the
  // voice-triggered target/radarRequest props below AND by picking a
  // search result, so it's the one source of truth Street View queries
  // against regardless of which path put the marker there.
  const [activePoint, setActivePoint] = useState<MapTarget>(DEFAULT_TARGET);

  // Street-level imagery (Mapillary). streetViewImageId is separate from
  // streetViewOn so the "find nearest image" effect and the "mount/update
  // the actual Viewer" effect can be two clean, independently-triggered
  // steps rather than one effect racing the container ref's mount timing.
  const [streetViewOn, setStreetViewOn] = useState(false);
  const [streetViewImageId, setStreetViewImageId] = useState<string | null>(null);
  const [streetViewLoading, setStreetViewLoading] = useState(false);
  const [streetViewError, setStreetViewError] = useState<string | null>(null);
  const streetViewContainerRef = useRef<HTMLDivElement>(null);
  const viewerRef = useRef<Viewer | null>(null);

  // Step 1: whenever street view opens (or the active point changes while
  // it's open), find the nearest real Mapillary image within 50m.
  useEffect(() => {
    if (!streetViewOn) return;
    if (!MAPILLARY_TOKEN) {
      setStreetViewError("Add VITE_MAPILLARY_TOKEN in .env sir -- free account at mapillary.com/dashboard/developers, no billing required.");
      return;
    }
    let cancelled = false;
    setStreetViewLoading(true);
    setStreetViewError(null);
    // closeto alone isn't enough -- verified directly against the real API,
    // it 400s with "'lat' and 'lng' parameters are required when 'radius'
    // is provided" unless lat/lng are ALSO passed separately (undocumented
    // in the obvious place, an actual Graph API quirk). radius is capped
    // at 50 by the API itself (a larger value 400s too), so 50 is already
    // the widest search this endpoint allows.
    fetch(`https://graph.mapillary.com/images?access_token=${MAPILLARY_TOKEN}&fields=id&closeto=${activePoint.lon},${activePoint.lat}&lat=${activePoint.lat}&lng=${activePoint.lon}&radius=50&limit=1`)
      .then(res => res.json())
      .then(data => {
        if (cancelled) return;
        if (data?.error) { setStreetViewError(`Mapillary API error sir: ${data.error.message || "unknown error"}`); return; }
        const imageId = data?.data?.[0]?.id as string | undefined;
        if (!imageId) { setStreetViewError("No street-level imagery within 50m of this spot sir."); return; }
        setStreetViewImageId(imageId);
      })
      .catch(() => { if (!cancelled) setStreetViewError("Couldn't reach Mapillary sir."); })
      .finally(() => { if (!cancelled) setStreetViewLoading(false); });
    return () => { cancelled = true; };
  }, [streetViewOn, activePoint]);

  // Step 2: once we have a real image id AND the container div is actually
  // mounted, create the Viewer (once) or move it to a new image (every
  // time the active point changes while street view stays open).
  useEffect(() => {
    if (!streetViewOn || !streetViewImageId || !streetViewContainerRef.current) return;
    if (viewerRef.current) {
      viewerRef.current.moveTo(streetViewImageId).catch(() => {});
    } else {
      viewerRef.current = new Viewer({
        accessToken: MAPILLARY_TOKEN,
        container: streetViewContainerRef.current,
        imageId: streetViewImageId,
      });
    }
  }, [streetViewOn, streetViewImageId]);

  // Tear down when street view closes, and on unmount.
  useEffect(() => {
    if (streetViewOn) return;
    if (viewerRef.current) { viewerRef.current.remove(); viewerRef.current = null; }
    setStreetViewImageId(null);
    setStreetViewError(null);
  }, [streetViewOn]);
  useEffect(() => () => { if (viewerRef.current) viewerRef.current.remove(); }, []);

  // Address search (Nominatim -- OpenStreetMap's free, keyless geocoder;
  // fits naturally since the map itself is already OSM-based). Purely
  // local/additive: doesn't touch the target/radarRequest props the
  // voice-command path uses.
  const [searchQuery, setSearchQuery] = useState("");
  const [searchResults, setSearchResults] = useState<MapTarget[]>([]);
  const [searching, setSearching] = useState(false);
  const searchDebounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const searchAbortRef = useRef<AbortController | null>(null);

  useEffect(() => {
    if (searchDebounceRef.current) clearTimeout(searchDebounceRef.current);
    const q = searchQuery.trim();
    if (q.length < 3) { setSearchResults([]); setSearching(false); return; }
    setSearching(true);
    searchDebounceRef.current = setTimeout(async () => {
      searchAbortRef.current?.abort();
      const controller = new AbortController();
      searchAbortRef.current = controller;
      try {
        const res = await fetch(
          `https://nominatim.openstreetmap.org/search?format=json&limit=5&q=${encodeURIComponent(q)}`,
          { signal: controller.signal }
        );
        const data = await res.json();
        setSearchResults((data || []).map((r: any) => ({
          lat: parseFloat(r.lat), lon: parseFloat(r.lon), name: r.display_name as string,
        })));
      } catch {
        // A stale/aborted request or a network hiccup -- leave whatever
        // results are already showing rather than clearing them out from
        // under the user mid-type.
      } finally {
        setSearching(false);
      }
    }, 400);
    return () => { if (searchDebounceRef.current) clearTimeout(searchDebounceRef.current); };
  }, [searchQuery]);

  const selectSearchResult = (r: MapTarget) => {
    const map = mapRef.current;
    map?.flyTo({ center: [r.lon, r.lat], zoom: 13, duration: 1100 });
    markerRef.current?.setLngLat([r.lon, r.lat]);
    setActivePoint(r);
    setSearchQuery("");
    setSearchResults([]);
  };

  // Map init -- once per mount.
  useEffect(() => {
    if (!containerRef.current || mapRef.current) return;
    const map = new maplibregl.Map({
      container: containerRef.current,
      style: STYLE_URL as any,
      center: [DEFAULT_TARGET.lon, DEFAULT_TARGET.lat],
      zoom: 10,
      attributionControl: { compact: true },
    });
    map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "bottom-right");
    map.on("load", () => setMapReady(true));
    markerRef.current = new maplibregl.Marker({ element: buildMarkerEl() })
      .setLngLat([DEFAULT_TARGET.lon, DEFAULT_TARGET.lat])
      .addTo(map);
    mapRef.current = map;

    // Full-bleed inside a flex page layout rather than a fixed-size card --
    // MapLibre caches its canvas size at mount/style-load time, so a nudge
    // on window resize (and once after the page's own open transition
    // settles) keeps it filling the container correctly.
    const onResize = () => map.resize();
    window.addEventListener("resize", onResize);
    const resizeTimer = setTimeout(onResize, 260);

    return () => {
      window.removeEventListener("resize", onResize);
      clearTimeout(resizeTimer);
      map.remove();
      mapRef.current = null;
      if (radarTimerRef.current) clearInterval(radarTimerRef.current);
    };
  }, []);

  // Fly to a newly-requested location.
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !target) return;
    map.flyTo({ center: [target.lon, target.lat], zoom: 10, duration: 1100 });
    markerRef.current?.setLngLat([target.lon, target.lat]);
    setActivePoint(target);
  }, [target]);

  const RADAR_SOURCE_ID = "rainviewer-radar";
  const RADAR_LAYER_ID = "rainviewer-radar-layer";

  const loadRadarFrame = async () => {
    const map = mapRef.current;
    if (!map) return;
    try {
      const res = await fetch("https://api.rainviewer.com/public/weather-maps.json");
      const data = await res.json();
      const frames = data?.radar?.past;
      const latest = frames && frames[frames.length - 1];
      if (!latest) return;
      const url = `${data.host}${latest.path}/256/{z}/{x}/{y}/2/1_1.png`;
      if (map.getLayer(RADAR_LAYER_ID)) map.removeLayer(RADAR_LAYER_ID);
      if (map.getSource(RADAR_SOURCE_ID)) map.removeSource(RADAR_SOURCE_ID);
      // RainViewer's radar mosaic only actually has tiles through z7 --
      // verified directly (z0-7 return real images, z8+ return a "Zoom
      // Level Not Supported" placeholder graphic, not an error). maxzoom
      // tells MapLibre to stop requesting tiles past that and instead
      // over-scale the z7 tile to cover the current viewport, same
      // standard technique used for any raster source with limited native
      // resolution -- this map's own default/radar zoom (8-11) was past
      // that limit, which is exactly why the placeholder was showing.
      map.addSource(RADAR_SOURCE_ID, { type: "raster", tiles: [url], tileSize: 256, maxzoom: 7 });
      map.addLayer({ id: RADAR_LAYER_ID, type: "raster", source: RADAR_SOURCE_ID, paint: { "raster-opacity": 0.55 } });
    } catch {
      // Radar is a nice-to-have overlay -- a failed fetch just leaves the
      // plain map showing, no error state needed for this.
    } finally {
      setRadarLoading(false);
    }
  };

  const enableRadar = () => {
    setRadarOn(true);
    setRadarLoading(true);
    if (mapReady) loadRadarFrame();
    if (radarTimerRef.current) clearInterval(radarTimerRef.current);
    radarTimerRef.current = setInterval(loadRadarFrame, 5 * 60 * 1000); // RainViewer publishes a new frame every ~10min; 5min keeps it fresh without hammering the API
  };

  const disableRadar = () => {
    setRadarOn(false);
    if (radarTimerRef.current) { clearInterval(radarTimerRef.current); radarTimerRef.current = null; }
    const map = mapRef.current;
    if (map?.getLayer(RADAR_LAYER_ID)) map.removeLayer(RADAR_LAYER_ID);
    if (map?.getSource(RADAR_SOURCE_ID)) map.removeSource(RADAR_SOURCE_ID);
  };

  // Voice-triggered radar requests (show_weather_radar) -- nonce means
  // this fires even when asking for radar on the same city as last time.
  useEffect(() => {
    if (!radarRequest) return;
    if (radarRequest.target) {
      const map = mapRef.current;
      map?.flyTo({ center: [radarRequest.target.lon, radarRequest.target.lat], zoom: 8, duration: 1100 });
      markerRef.current?.setLngLat([radarRequest.target.lon, radarRequest.target.lat]);
      setActivePoint(radarRequest.target);
    }
    enableRadar();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [radarRequest?.nonce]);

  return (
    <div className="w-map w-map--full">
      <div className="w-map__canvas" ref={containerRef}>
        <div className="w-map__overlay-label">
          <span className="w-sublabel">{radarOn ? "WEATHER RADAR" : "MAP"}</span>
          <span className="w-map__location">{shortName(activePoint.name)}</span>
        </div>

        <div className="w-map__search">
          <input
            className="w-map__search-input"
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            placeholder="Search an address…"
          />
          {searching && <div className="w-map__search-spinner" />}
          {searchResults.length > 0 && (
            <div className="w-map__search-results">
              {searchResults.map((r, i) => (
                <button key={i} className="w-map__search-result" onClick={() => selectSearchResult(r)}>
                  {r.name}
                </button>
              ))}
            </div>
          )}
        </div>

        <button
          className="w-map__toggle w-map__toggle--float"
          onClick={() => (radarOn ? disableRadar() : enableRadar())}
          title={radarOn ? "Switch to plain map" : "Show precipitation radar"}
        >
          {radarOn ? "MAP" : "RADAR"}
        </button>
        <button
          className="w-map__toggle w-map__toggle--float w-map__toggle--street"
          onClick={() => setStreetViewOn(v => !v)}
          title="Street-level imagery (Mapillary)"
        >
          {streetViewOn ? "MAP" : "STREET VIEW"}
        </button>
        {radarLoading && <div className="w-map__loading">LOADING RADAR…</div>}

        {streetViewOn && (
          <div className="w-map__street-view">
            <div className="w-map__street-view-header">
              <span className="w-sublabel">STREET VIEW</span>
              <button className="w-map__street-view-close" onClick={() => setStreetViewOn(false)}>✕</button>
            </div>
            <div ref={streetViewContainerRef} className="w-map__street-view-canvas" />
            {streetViewLoading && <div className="w-map__loading">LOADING STREET VIEW…</div>}
            {streetViewError && <div className="w-map__street-view-error">{streetViewError}</div>}
          </div>
        )}
      </div>
    </div>
  );
}
