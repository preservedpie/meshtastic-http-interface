const { useState, useEffect, useCallback, useRef } = React;

// ── Helpers ──────────────────────────────────────────────────────────────────

// Parse the two timestamp formats this app produces:
//   - packets DB:  "2026-06-02 14:30:00"            (space, no zone → treat as UTC)
//   - bot DB:      "2026-06-02T14:30:00.123+00:00"  (ISO 8601 with offset)
function parseTs(ts) {
  if (!ts) return null;
  // If the string already carries timezone info (offset or trailing Z), trust it.
  const hasZone = /[zZ]$|[+-]\d{2}:?\d{2}$/.test(ts);
  const iso = hasZone ? ts.replace(' ', 'T') : ts.replace(' ', 'T') + 'Z';
  const d = new Date(iso);
  return isNaN(d.getTime()) ? null : d;
}

function fmtTs(ts) {
  const d = parseTs(ts);
  if (!d) return '—';
  const diff = Math.floor((Date.now() - d.getTime()) / 1000);
  const future = diff < 0;
  const abs = Math.abs(diff);
  if (abs < 60)   return future ? `in ${abs}s` : `${abs}s ago`;
  if (abs < 3600) return future ? `in ${Math.floor(abs / 60)}m` : `${Math.floor(abs / 60)}m ago`;
  if (abs < 86400)return future ? `in ${Math.floor(abs / 3600)}h` : `${Math.floor(abs / 3600)}h ago`;
  return d.toLocaleDateString();
}

function fmtFull(ts) {
  const d = parseTs(ts);
  return d ? d.toLocaleString() : '—';
}

function typeBadge(type) {
  const cls = `badge badge-${type || 'unknown'}`;
  return <span className={cls}>{type || 'unknown'}</span>;
}

async function apiFetch(path, opts) {
  const res = await fetch(path, opts);
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json();
}

// useState that survives reloads: mirrors the value to localStorage under
// "pref:<key>". Used for UI preferences (selected node, ranges, checkboxes)
// so the app reopens the way the user left it.
function usePersistentState(key, initial) {
  const [val, setVal] = useState(() => {
    try {
      const raw = localStorage.getItem('pref:' + key);
      return raw != null ? JSON.parse(raw) : initial;
    } catch (_) {
      return initial;
    }
  });
  useEffect(() => {
    try { localStorage.setItem('pref:' + key, JSON.stringify(val)); } catch (_) {}
  }, [key, val]);
  return [val, setVal];
}

// Friendly display name for a node row: alias > long name > short name > id.
function nodeDisplayName(n) {
  if (!n) return '';
  return n.alias || n.long_name || n.short_name || n.node_id;
}

// ── Temperature unit (°C/°F) ──────────────────────────────────────────────────
// Stored telemetry is always Celsius; we convert at display time. The chosen
// unit lives in localStorage and is shared app-wide via TempUnitContext.
const TempUnitContext = React.createContext('C');

function cToF(c) { return c * 9 / 5 + 32; }

// Convert a Celsius value to the active unit. Non-temperature values pass through.
function convertTemp(celsius, unit) {
  if (celsius == null) return celsius;
  return unit === 'F' ? cToF(celsius) : celsius;
}

function tempUnitLabel(unit) { return unit === 'F' ? '°F' : '°C'; }

// One decimal place, trimmed.
function fmtTemp(celsius, unit) {
  if (celsius == null) return '—';
  return `${Math.round(convertTemp(celsius, unit) * 10) / 10}${tempUnitLabel(unit)}`;
}

// For a charted metric, apply temperature conversion to a {t,v}[] series and
// return the unit label to display. Non-temperature metrics pass through with
// their static unit from METRICS.
function applyUnit(metric, series, baseUnit, unit) {
  if (metric !== 'temperature') return { series, unit: baseUnit };
  return {
    series: (series || []).map(p => ({ t: p.t, v: convertTemp(p.v, unit) })),
    unit: tempUnitLabel(unit),
  };
}

// ── Filter bar ────────────────────────────────────────────────────────────────

function FilterBar({ filters, onChange }) {
  const set = (k) => (e) => onChange({ ...filters, [k]: e.target.value });
  return (
    <div className="filter-bar">
      <div className="filter-group">
        <label>Type</label>
        <select value={filters.type} onChange={set('type')}>
          <option value="">All</option>
          {['text','telemetry','nodeinfo','position','traceroute','neighborinfo','map_report','unknown'].map(t =>
            <option key={t} value={t}>{t}</option>
          )}
        </select>
      </div>
      <span className="filter-sep">|</span>
      <div className="filter-group">
        <label>Node</label>
        <input
          type="text"
          placeholder="!abc1234"
          value={filters.node}
          onChange={set('node')}
          style={{ width: 110 }}
        />
      </div>
      <span className="filter-sep">|</span>
      <div className="filter-group">
        <label>Channel</label>
        <input
          type="text"
          placeholder="index, e.g. 0"
          value={filters.channel}
          onChange={set('channel')}
          style={{ width: 90 }}
        />
      </div>
      <span className="filter-sep">|</span>
      <div className="filter-group">
        <label>Window</label>
        <select value={filters.since} onChange={set('since')}>
          <option value="">All time</option>
          <option value="1h">1h</option>
          <option value="6h">6h</option>
          <option value="24h">24h</option>
          <option value="7d">7d</option>
        </select>
      </div>
    </div>
  );
}

// ── Live Feed ─────────────────────────────────────────────────────────────────

function LiveFeed({ filters }) {
  const [packets, setPackets] = useState([]);
  const [error, setError]   = useState(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(() => {
    const p = new URLSearchParams();
    if (filters.type)    p.set('type',    filters.type);
    if (filters.node)    p.set('node',    filters.node);
    if (filters.channel) p.set('channel', filters.channel);
    if (filters.since)   p.set('since',   filters.since);
    p.set('limit', '200');
    apiFetch(`/api/packets?${p}`)
      .then(d => { setPackets(d); setError(null); })
      .catch(e => setError(e.message))
      .finally(() => setLoading(false));
  }, [filters.type, filters.node, filters.channel, filters.since]);

  useEffect(() => { load(); }, [load]);

  // auto-refresh every 5s
  useEffect(() => {
    const id = setInterval(load, 5000);
    return () => clearInterval(id);
  }, [load]);

  if (loading) return <div className="empty">Loading…</div>;

  return (
    <div>
      {error && <div className="error-banner">Error: {error}</div>}
      <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', marginBottom: '0.625rem' }}>
        <span style={{ fontSize: '0.8125rem', color: 'var(--muted)' }}>
          {packets.length} packet{packets.length !== 1 ? 's' : ''}
        </span>
        <span className="auto-badge">auto-refresh 5s</span>
      </div>
      {packets.length === 0
        ? <div className="empty">No packets — waiting for data…</div>
        : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Time</th>
                  <th>Type</th>
                  <th>From</th>
                  <th>Channel</th>
                  <th>RSSI</th>
                  <th>SNR</th>
                  <th>Hops</th>
                  <th>Summary</th>
                </tr>
              </thead>
              <tbody>
                {packets.map(p => (
                  <tr key={p.id}>
                    <td className="mono muted" style={{ whiteSpace: 'nowrap' }}>{fmtTs(p.received_at)}</td>
                    <td>{typeBadge(p.packet_type)}</td>
                    <td className="mono" style={{ color: 'var(--accent)', whiteSpace: 'nowrap' }}>{p.from_node || '—'}</td>
                    <td>{p.channel || '—'}</td>
                    <td className="mono">{p.rssi != null ? p.rssi : '—'}</td>
                    <td className="mono">{p.snr  != null ? p.snr  : '—'}</td>
                    <td className="mono">{p.hops_away != null ? p.hops_away : '—'}</td>
                    <td style={{ maxWidth: 260, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                      <PacketSummary packet={p} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )
      }
    </div>
  );
}

function PacketSummary({ packet }) {
  const unit = React.useContext(TempUnitContext);
  if (!packet.payload_json) return null;
  try {
    const raw = typeof packet.payload_json === 'string'
      ? JSON.parse(packet.payload_json)
      : packet.payload_json;
    const payload = raw.payload || {};
    if (packet.packet_type === 'text')
      return <span style={{ color: 'var(--text)' }}>{payload.text || ''}</span>;
    if (packet.packet_type === 'telemetry') {
      const parts = [];
      if (payload.battery_level != null) parts.push(`🔋 ${payload.battery_level}%`);
      if (payload.voltage       != null) parts.push(`${payload.voltage}V`);
      if (payload.temperature   != null) parts.push(`🌡 ${fmtTemp(payload.temperature, unit)}`);
      return <span className="muted">{parts.join(' · ') || 'telemetry'}</span>;
    }
    if (packet.packet_type === 'nodeinfo')
      return <span className="muted">{payload.longname || payload.shortname || 'nodeinfo'}</span>;
    if (packet.packet_type === 'position')
      return <span className="muted">{payload.latitude_i ? `${(payload.latitude_i/1e7).toFixed(4)}, ${(payload.longitude_i/1e7).toFixed(4)}` : 'position'}</span>;
  } catch (_) {}
  return <span className="muted">{packet.packet_type}</span>;
}

// ── Nodes ─────────────────────────────────────────────────────────────────────

function NodeActions({ nodeId, available }) {
  const [states, setStates] = useState({});  // { telemetry: 'idle'|'pending'|'ok'|'err', ... }

  const request = async (type) => {
    setStates(s => ({ ...s, [type]: 'pending' }));
    try {
      const res = await fetch(`/api/nodes/${encodeURIComponent(nodeId)}/request/${type}`, { method: 'POST' });
      if (!res.ok) throw new Error(`${res.status}`);
      setStates(s => ({ ...s, [type]: 'ok' }));
      setTimeout(() => setStates(s => ({ ...s, [type]: 'idle' })), 3000);
    } catch (_) {
      setStates(s => ({ ...s, [type]: 'err' }));
      setTimeout(() => setStates(s => ({ ...s, [type]: 'idle' })), 3000);
    }
  };

  if (!available) return null;

  const btn = (type, label) => {
    const st = states[type] || 'idle';
    return (
      <button
        key={type}
        className={`btn btn-ghost btn-sm${st === 'ok' ? ' btn-ok' : st === 'err' ? ' btn-danger' : ''}`}
        style={{ marginRight: 3 }}
        disabled={st === 'pending'}
        onClick={() => request(type)}
        title={`Request ${label} from this node`}
      >
        {st === 'pending' ? '…' : st === 'ok' ? '✓' : label}
      </button>
    );
  };

  return (
    <span style={{ whiteSpace: 'nowrap' }}>
      {btn('telemetry',  'Telemetry')}
      {btn('position',   'Position')}
      {btn('traceroute', 'Traceroute')}
    </span>
  );
}

function TelemetryBadge({ data }) {
  const unit = React.useContext(TempUnitContext);
  if (!data) return <span className="muted">—</span>;
  const parts = [];
  if (data.battery_level != null) {
    const pct = data.battery_level;
    const color = pct > 50 ? 'var(--green)' : pct > 20 ? '#facc15' : 'var(--danger)';
    parts.push(<span key="bat" style={{ color }}>🔋{pct}%</span>);
  }
  if (data.voltage != null)       parts.push(<span key="v"   className="muted">{data.voltage.toFixed(2)}V</span>);
  if (data.temperature != null)   parts.push(<span key="tmp" className="muted">{fmtTemp(data.temperature, unit)}</span>);
  if (data.relative_humidity != null) parts.push(<span key="hum" className="muted">{data.relative_humidity.toFixed(0)}%RH</span>);
  if (data.channel_utilization != null) parts.push(<span key="ch" className="muted">ch:{data.channel_utilization.toFixed(1)}%</span>);
  if (parts.length === 0) return <span className="muted">—</span>;
  return <span style={{ display: 'flex', gap: '0.35rem', flexWrap: 'wrap' }}>{parts}</span>;
}

// Inline-editable friendly name for a node. Click ✎ to edit; Enter saves.
function AliasCell({ node, onSaved }) {
  const [editing, setEditing] = useState(false);
  const [val, setVal] = useState(node.alias || '');
  const [busy, setBusy] = useState(false);

  const save = () => {
    setBusy(true);
    apiFetch(`/api/nodes/${encodeURIComponent(node.node_id)}/alias`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ alias: val.trim() }),
    })
      .then(updated => { setEditing(false); onSaved && onSaved(updated); })
      .catch(() => {})
      .finally(() => setBusy(false));
  };

  if (editing) {
    return (
      <span className="alias-edit">
        <input
          autoFocus
          value={val}
          disabled={busy}
          placeholder="friendly name"
          onChange={e => setVal(e.target.value)}
          onKeyDown={e => {
            if (e.key === 'Enter') save();
            if (e.key === 'Escape') { setVal(node.alias || ''); setEditing(false); }
          }}
          style={{ width: 120 }}
        />
        <button className="link-btn" onClick={save} disabled={busy}>✓</button>
      </span>
    );
  }

  return (
    <span className="alias-cell">
      {node.alias
        ? <span style={{ color: 'var(--accent)' }}>{node.alias}</span>
        : <span className="muted">—</span>}
      <button className="link-btn" title="Set friendly name" onClick={() => setEditing(true)}>✎</button>
    </span>
  );
}

function NodeFlag({ on, label, title }) {
  if (!on) return null;
  return <span className="node-flag" title={title}>{label}</span>;
}

function NodeDetailsModal({ nodeId, fallback, onClose }) {
  const [detail, setDetail] = useState(null);
  const [error,  setError]  = useState(null);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    let alive = true;
    apiFetch(`/api/nodes/${encodeURIComponent(nodeId)}`)
      .then(d => { if (alive) setDetail(d); })
      .catch(e => { if (alive) setError(e.message); });
    return () => { alive = false; };
  }, [nodeId]);

  useEffect(() => {
    const onKey = e => { if (e.key === 'Escape') onClose(); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  const n = (detail && detail.node) || fallback || {};
  const packets = (detail && detail.recent_packets) || [];

  const copyKey = () => {
    if (!n.public_key || !navigator.clipboard) return;
    navigator.clipboard.writeText(n.public_key)
      .then(() => { setCopied(true); setTimeout(() => setCopied(false), 2000); })
      .catch(() => {});
  };

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal-panel" onClick={e => e.stopPropagation()}>
        <div className="modal-header">
          <h3>{n.alias || n.long_name || n.node_id}</h3>
          <button className="link-btn" onClick={onClose}>✕</button>
        </div>
        {error && <div className="error-banner">{error}</div>}

        <div className="node-detail-grid">
          <div><span className="dt-label">Node ID</span><span className="mono">{n.node_id || '—'}</span></div>
          <div><span className="dt-label">Node Num</span><span className="mono">{n.node_num ?? '—'}</span></div>
          <div><span className="dt-label">Long Name</span>{n.long_name || <span className="muted">—</span>}</div>
          <div><span className="dt-label">Short Name</span><span className="mono">{n.short_name || '—'}</span></div>
          <div><span className="dt-label">Alias</span>{n.alias || <span className="muted">—</span>}</div>
          <div><span className="dt-label">Hardware</span>{n.hw_model || <span className="muted">—</span>}</div>
          <div><span className="dt-label">Role</span>{n.role || <span className="muted">—</span>}</div>
          <div><span className="dt-label">MAC Address</span><span className="mono">{n.macaddr || '—'}</span></div>
          <div><span className="dt-label">Last Seen</span>{n.last_seen ? fmtFull(n.last_seen) : '—'}</div>
          <div><span className="dt-label">RSSI / SNR</span><span className="mono">{n.last_rssi ?? '—'} / {n.last_snr ?? '—'}</span></div>
          <div><span className="dt-label">Hops Away</span><span className="mono">{n.last_hops ?? '—'}</span></div>
        </div>

        <div className="node-flags-row">
          <NodeFlag on={n.is_favorite}              label="★ Favorite" />
          <NodeFlag on={n.is_key_manually_verified}  label="✓ Key Verified" title="Public key has been manually verified out-of-band" />
          <NodeFlag on={n.is_licensed}               label="Licensed (HAM)" />
          <NodeFlag on={n.via_mqtt}                  label="Via MQTT" title="Last heard through an MQTT bridge, not direct RF" />
          <NodeFlag on={n.is_unmessagable}           label="⚠ Unmessagable" title="This node has marked itself as not accepting DMs" />
        </div>

        <div className="dt-label" style={{ marginTop: '0.5rem' }}>Public Key (PKI)</div>
        {n.public_key ? (
          <div className="pubkey-box">
            <code className="pubkey-value">{n.public_key}</code>
            <button className="btn btn-ghost btn-sm" onClick={copyKey}>{copied ? 'Copied' : 'Copy'}</button>
          </div>
        ) : (
          <div className="pubkey-missing">
            No public key on file yet — direct messages to this node will fail with
            <code> PKI_UNKNOWN_PUBKEY</code> until one is learned (it arrives on a NODEINFO
            announcement from the node itself).
          </div>
        )}

        {packets.length > 0 && (
          <React.Fragment>
            <div className="dt-label" style={{ marginTop: '0.75rem' }}>Recent Packets</div>
            <div className="node-recent-packets">
              {packets.slice(0, 15).map(p => (
                <div key={p.id} className="node-recent-packet-row">
                  <span className="muted" style={{ whiteSpace: 'nowrap' }} title={fmtFull(p.received_at)}>{fmtTs(p.received_at)}</span>
                  {typeBadge(p.packet_type)}
                  <PacketSummary packet={p} />
                </div>
              ))}
            </div>
          </React.Fragment>
        )}
      </div>
    </div>
  );
}

function Nodes() {
  const [nodes,    setNodes]    = useState([]);
  const [telemetry, setTelemetry] = useState({});
  const [serialOk, setSerialOk] = useState(false);
  const [error,    setError]    = useState(null);
  const [loading,  setLoading]  = useState(true);
  const [zeroHopsOnly, setZeroHopsOnly] = usePersistentState('nodes.zeroHopsOnly', false);
  const [detailNode, setDetailNode] = useState(null);

  useEffect(() => {
    Promise.all([
      apiFetch('/api/nodes'),
      apiFetch('/api/nodes/telemetry'),
      apiFetch('/api/status'),
    ])
      .then(([n, t, s]) => {
        setNodes(n);
        setTelemetry(t);
        setSerialOk(!!s.serial_connected);
        setError(null);
      })
      .catch(e => setError(e.message))
      .finally(() => setLoading(false));
  }, []);

  if (loading) return <div className="empty">Loading…</div>;

  // A node is "0 hops" (direct RF neighbor) when its last packet arrived with hops_away === 0.
  const visible = zeroHopsOnly ? nodes.filter(n => n.last_hops === 0) : nodes;

  const onAliasSaved = (updated) =>
    setNodes(prev => prev.map(n => n.node_id === updated.node_id ? { ...n, alias: updated.alias } : n));

  return (
    <div>
      {error && <div className="error-banner">Error: {error}</div>}
      <div style={{ display: 'flex', gap: '0.75rem', alignItems: 'center', marginBottom: '0.625rem' }}>
        <span style={{ color: 'var(--muted)', fontSize: '0.8125rem' }}>
          {visible.length}{zeroHopsOnly ? ` of ${nodes.length}` : ''} node{visible.length !== 1 ? 's' : ''}
        </span>
        <label className="hop-toggle">
          <input type="checkbox" checked={zeroHopsOnly} onChange={e => setZeroHopsOnly(e.target.checked)} />
          0 hops only (direct)
        </label>
        {serialOk
          ? <span className="auto-badge" style={{ background: 'var(--green)', color: '#000' }}>Serial connected</span>
          : <span className="auto-badge" style={{ background: 'var(--border)', color: 'var(--muted)' }}>Serial offline</span>}
      </div>
      {visible.length === 0
        ? <div className="empty">{zeroHopsOnly ? 'No direct (0-hop) nodes heard' : 'No nodes seen yet'}</div>
        : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Node ID</th>
                  <th>Friendly Name</th>
                  <th>Long Name</th>
                  <th>Short</th>
                  <th>Hardware</th>
                  <th>Role</th>
                  <th>Last Seen</th>
                  <th>RSSI</th>
                  <th>SNR</th>
                  <th>Hops</th>
                  <th>Telemetry</th>
                  <th>Details</th>
                  {serialOk && <th>Request</th>}
                </tr>
              </thead>
              <tbody>
                {visible.map(n => (
                  <tr key={n.node_id}>
                    <td className="mono" style={{ color: 'var(--accent)' }}>{n.node_id}</td>
                    <td><AliasCell node={n} onSaved={onAliasSaved} /></td>
                    <td>{n.long_name  || <span className="muted">—</span>}</td>
                    <td className="mono">{n.short_name || <span className="muted">—</span>}</td>
                    <td className="muted">{n.hw_model || '—'}</td>
                    <td className="muted">{n.role     || '—'}</td>
                    <td className="muted" style={{ whiteSpace: 'nowrap' }} title={fmtFull(n.last_seen)}>{fmtTs(n.last_seen)}</td>
                    <td className="mono">{n.last_rssi != null ? n.last_rssi : '—'}</td>
                    <td className="mono">{n.last_snr  != null ? n.last_snr  : '—'}</td>
                    <td className="mono">{n.last_hops != null ? n.last_hops : '—'}</td>
                    <td style={{ minWidth: 140 }}><TelemetryBadge data={telemetry[n.node_id]} /></td>
                    <td>
                      <button className="btn btn-ghost btn-sm" onClick={() => setDetailNode(n)}>
                        {n.public_key ? 'Details 🔑' : 'Details'}
                      </button>
                    </td>
                    {serialOk && <td><NodeActions nodeId={n.node_id} available={serialOk} /></td>}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )
      }
      {detailNode && (
        <NodeDetailsModal
          nodeId={detailNode.node_id}
          fallback={detailNode}
          onClose={() => setDetailNode(null)}
        />
      )}
    </div>
  );
}

// ── Messages ──────────────────────────────────────────────────────────────────

// Fallback used until the real device channel list loads (or if the radio
// is unreachable) — see Compose's /api/device/channels fetch below.
const CHANNELS = [
  { label: 'Channel 0', value: 0 },
];

// Human-readable hints for the Routing.Error codes the firmware reports back
// on a *confirmed* failed DM (a NAK actually came back), keyed to match
// meshtastic_iface._send_with_ack's `reason`. ack_timeout is deliberately not
// here — it means "no confirmation either way", not a failure (see
// ACK_UNCONFIRMED_HINTS below), and the backend reports it with ok:true.
const ACK_ERROR_HINTS = {
  PKI_UNKNOWN_PUBKEY: "this node's public key isn't known yet — it needs to announce (NODEINFO) or message this radio first, then retry",
  NO_ROUTE:            'no route to that node',
  TIMEOUT:              'timed out waiting for a response',
  MAX_RETRANSMIT:       'max retransmissions reached — node may be out of range',
  GOT_NAK:               'node rejected the packet',
  PKI_FAILED:            'PKI encryption failed',
};

// Reason-specific text for a "sent, but delivery unconfirmed" result
// (ok:true, acked:false) — this is not a failure, just an unknown.
const ACK_UNCONFIRMED_HINTS = {
  ack_timeout:        'no ACK heard back in time — on a multi-hop mesh that often just means the ACK didn’t make it back, not that the message failed',
  no_confirm_support:  "this radio's library can't report delivery confirmation",
  sent_no_confirm:     "sent without requesting delivery confirmation",
};

function Compose({ onSent }) {
  const [text,    setText]    = useState('');
  const [channel, setChannel] = usePersistentState('compose.channel', 0);
  const [mode,    setMode]    = usePersistentState('compose.mode', 'broadcast'); // 'broadcast' | 'dm'
  const [toNode,  setToNode]  = usePersistentState('compose.toNode', '');
  const [nodes,   setNodes]   = useState([]);
  const [channels, setChannels] = useState(CHANNELS);
  const [status,  setStatus]  = useState(null);  // null | 'sending' | 'ok' | 'ok-noack' | string(error)
  const [lastReason, setLastReason] = useState(null);

  useEffect(() => {
    apiFetch('/api/nodes')
      .then(d => setNodes(Array.isArray(d) ? d : []))
      .catch(() => {});
  }, []);

  // Broadcast channel choices come from the radio's actual channel table —
  // not a static guess — so a channel added/renamed on the device (see the
  // Local Node > Channels page) shows up here too. Disabled slots are
  // excluded since you can't send on them. Falls back to CHANNELS (just
  // "Channel 0") if the radio's unreachable, so composing still works.
  useEffect(() => {
    let alive = true;
    const load = () => {
      apiFetch('/api/device/channels')
        .then(d => {
          if (!alive || !Array.isArray(d)) return;
          const active = d
            .filter(c => c.role !== 'DISABLED')
            .map(c => ({ value: c.index, label: c.name || `Channel ${c.index}` }));
          if (!active.length) return;
          setChannels(active);
          setChannel(prev => (active.some(c => c.value === prev) ? prev : active[0].value));
        })
        .catch(() => {});
    };
    load();
    const id = setInterval(load, 15000);
    return () => { alive = false; clearInterval(id); };
  }, []);

  const send = async () => {
    if (!text.trim()) return;
    if (mode === 'dm' && !toNode.trim()) { setStatus('choose a destination node'); return; }
    setStatus('sending');
    try {
      const body = mode === 'dm'
        ? { text: text.trim(), to: toNode.trim() }
        : { text: text.trim(), channel };
      const res = await fetch('/api/send', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || res.statusText);
      if (mode === 'dm' && data.ok === false) {
        const hint = ACK_ERROR_HINTS[data.reason];
        throw new Error(hint ? `not delivered (${data.reason}): ${hint}` : `not delivered (${data.reason || 'unknown'})`);
      }
      setText('');
      setLastReason(data.reason || null);
      setStatus(mode === 'dm' && data.acked ? 'ok' : (mode === 'dm' ? 'ok-noack' : 'ok'));
      setTimeout(() => setStatus(null), 4000);
      if (onSent) onSent();
    } catch (e) {
      setStatus(e.message);
    }
  };

  const onKey = (e) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
  };

  const remaining = 200 - text.length;
  const canSend = !!text.trim() && status !== 'sending' && (mode !== 'dm' || !!toNode.trim());
  const dmTarget = mode === 'dm' && toNode.trim()
    ? nodes.find(n => n.node_id.toLowerCase() === toNode.trim().toLowerCase())
    : null;

  return (
    <div className="compose">
      <div className="compose-mode-toggle">
        <button
          type="button"
          className={mode === 'broadcast' ? 'active' : ''}
          onClick={() => setMode('broadcast')}
        >
          Broadcast
        </button>
        <button
          type="button"
          className={mode === 'dm' ? 'active' : ''}
          onClick={() => setMode('dm')}
        >
          Direct Message
        </button>
      </div>
      <div className="compose-row">
        {mode === 'broadcast' ? (
          <div className="compose-channel-wrap">
            <label>Channel</label>
            <select
              className="compose-channel"
              value={channel}
              onChange={e => setChannel(Number(e.target.value))}
            >
              {channels.map(c => (
                <option key={c.value} value={c.value}>{c.label}</option>
              ))}
            </select>
          </div>
        ) : (
          <div className="compose-channel-wrap">
            <label>To node</label>
            <input
              className="compose-channel compose-to"
              list="compose-node-list"
              value={toNode}
              onChange={e => setToNode(e.target.value)}
              placeholder="!a1b2c3d4"
            />
            <datalist id="compose-node-list">
              {nodes.map(n => (
                <option key={n.node_id} value={n.node_id}>
                  {n.long_name || n.short_name || n.node_id}
                </option>
              ))}
            </datalist>
          </div>
        )}
        <textarea
          className="compose-input"
          rows={2}
          placeholder={mode === 'dm' ? 'Send a direct message… (Enter to send)' : 'Send a message to the mesh… (Enter to send)'}
          value={text}
          onChange={e => setText(e.target.value)}
          onKeyDown={onKey}
          maxLength={200}
          disabled={status === 'sending'}
        />
        <button
          className="btn btn-primary compose-send"
          onClick={send}
          disabled={!canSend}
        >
          {status === 'sending' ? '…' : 'Send'}
        </button>
      </div>
      <div className="compose-footer">
        <span className={`compose-count ${remaining < 20 ? 'compose-count-warn' : ''}`}>
          {remaining} chars left
        </span>
        {status === 'ok'       && <span className="compose-ok">{mode === 'dm' ? 'Delivered ✓' : 'Sent'}</span>}
        {status === 'ok-noack' && (
          <span className="compose-ok" title={ACK_UNCONFIRMED_HINTS[lastReason] || ''}>
            Sent — delivery unconfirmed{ACK_UNCONFIRMED_HINTS[lastReason] ? ` (${lastReason})` : ''}
          </span>
        )}
        {status && status !== 'ok' && status !== 'ok-noack' && status !== 'sending' && (
          <span className="compose-err">{status}</span>
        )}
      </div>
      {dmTarget && !dmTarget.public_key && (
        <div className="compose-warn">
          No public key on file for this node yet — the DM may fail with PKI_UNKNOWN_PUBKEY
          until one is learned from it.
        </div>
      )}
    </div>
  );
}

const MESSAGE_SORT_COLUMNS = [
  { key: 'received_at', label: 'Time' },
  { key: 'direction',   label: 'Dir' },
  { key: 'from_node',   label: 'From' },
  { key: 'to_node',     label: 'To / Channel' },
  { key: 'message',     label: 'Message' },
  { key: 'rssi',        label: 'RSSI' },
  { key: 'snr',         label: 'SNR' },
  { key: 'hops_away',   label: 'Hops' },
  { key: 'bot_responded', label: 'Bot reply' },
  { key: 'rule_matched',  label: 'Forwarded' },
];

function Messages({ filters }) {
  const [messages, setMessages] = useState([]);
  const [error, setError]       = useState(null);
  const [loading, setLoading]   = useState(true);
  const [direction, setDirection] = usePersistentState('messages.direction', '');   // '' | 'in' | 'out'
  const [searchInput, setSearchInput] = useState('');
  const [search, setSearch]     = useState('');
  const [sort,  setSort]        = usePersistentState('messages.sort', 'received_at');
  const [order, setOrder]       = usePersistentState('messages.order', 'desc');

  // Debounce the search box so it doesn't refetch on every keystroke.
  useEffect(() => {
    const id = setTimeout(() => setSearch(searchInput.trim()), 300);
    return () => clearTimeout(id);
  }, [searchInput]);

  const load = useCallback(() => {
    const p = new URLSearchParams();
    if (filters.node)    p.set('node',      filters.node);
    if (filters.channel) p.set('channel',   filters.channel);
    if (filters.since)   p.set('since',     filters.since);
    if (direction)        p.set('direction', direction);
    if (search)            p.set('q',         search);
    p.set('sort',  sort);
    p.set('order', order);
    p.set('limit', '200');
    apiFetch(`/api/messages?${p}`)
      .then(d => { setMessages(d); setError(null); })
      .catch(e => setError(e.message))
      .finally(() => setLoading(false));
  }, [filters.node, filters.channel, filters.since, direction, search, sort, order]);

  useEffect(() => { load(); }, [load]);

  useEffect(() => {
    const id = setInterval(load, 10000);
    return () => clearInterval(id);
  }, [load]);

  const toggleSort = (key) => {
    if (sort === key) {
      setOrder(order === 'asc' ? 'desc' : 'asc');
    } else {
      setSort(key);
      setOrder(key === 'received_at' ? 'desc' : 'asc');
    }
  };

  if (loading) return <div className="empty">Loading…</div>;

  return (
    <div>
      <Compose onSent={() => setTimeout(load, 1500)} />
      {error && <div className="error-banner">Error: {error}</div>}

      <div className="messages-toolbar">
        <div className="compose-mode-toggle">
          <button type="button" className={direction === '' ? 'active' : ''} onClick={() => setDirection('')}>All</button>
          <button type="button" className={direction === 'in' ? 'active' : ''} onClick={() => setDirection('in')}>Incoming</button>
          <button type="button" className={direction === 'out' ? 'active' : ''} onClick={() => setDirection('out')}>Outgoing</button>
        </div>
        <input
          type="text"
          className="messages-search"
          placeholder="Search message text…"
          value={searchInput}
          onChange={e => setSearchInput(e.target.value)}
        />
        <span style={{ color: 'var(--muted)', fontSize: '0.8125rem' }}>
          {messages.length} message{messages.length !== 1 ? 's' : ''}
        </span>
        <span className="auto-badge">auto-refresh 10s</span>
      </div>

      {messages.length === 0
        ? <div className="empty">No messages yet</div>
        : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  {MESSAGE_SORT_COLUMNS.map(c => (
                    <th key={c.key} className="sortable-th" onClick={() => toggleSort(c.key)}>
                      {c.label}{sort === c.key && <span className="sort-arrow">{order === 'asc' ? ' ▲' : ' ▼'}</span>}
                    </th>
                  ))}
                  <th>Ack</th>
                </tr>
              </thead>
              <tbody>
                {messages.map(m => (
                  <tr key={m.id}>
                    <td className="mono muted" style={{ whiteSpace: 'nowrap' }} title={fmtFull(m.received_at)}>{fmtTs(m.received_at)}</td>
                    <td>
                      <span className={`dir-badge dir-${m.direction || 'in'}`}>
                        {m.direction === 'out' ? '⇨ out' : '⇦ in'}
                      </span>
                    </td>
                    <td className="mono" style={{ color: 'var(--accent)' }}>{m.from_node || <span className="muted">—</span>}</td>
                    <td className="mono">
                      {m.to_node
                        ? <span title="Direct message">DM → {m.to_node}</span>
                        : (m.channel != null && m.channel !== ''
                          ? <span>#{m.channel}</span>
                          : <span className="muted">broadcast</span>)}
                    </td>
                    <td style={{ maxWidth: 320, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{m.message}</td>
                    <td className="mono">{m.rssi != null ? m.rssi : '—'}</td>
                    <td className="mono">{m.snr  != null ? m.snr  : '—'}</td>
                    <td className="mono">{m.hops_away != null ? m.hops_away : '—'}</td>
                    <td className="mono" title={m.direction === 'in' ? 'Whether the bot replied to this message' : ''}>
                      {m.direction === 'in'
                        ? (m.bot_responded === 1 ? <span style={{ color: 'var(--success)' }}>✓</span> : <span className="muted">—</span>)
                        : <span className="muted">—</span>}
                    </td>
                    <td className="mono" title={m.rule_matched || ''}>
                      {m.rule_matched ? <span style={{ color: 'var(--accent)' }}>{m.rule_matched}</span> : <span className="muted">—</span>}
                    </td>
                    <td className="mono">
                      {m.direction === 'out' && m.to_node
                        ? (m.ack === 1 ? <span style={{ color: 'var(--success)' }}>✓</span>
                          : m.ack === 0 ? <span style={{ color: 'var(--danger)' }}>✗</span>
                          : <span className="muted">…</span>)
                        : <span className="muted">—</span>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )
      }
    </div>
  );
}

// ── Rules ─────────────────────────────────────────────────────────────────────

const BLANK_RULE = { name: '', enabled: true, packet_type: '', from_node: '', channel: '', keyword: '', priority: 0, sound: 'pushover' };

function Rules() {
  const [rules, setRules]       = useState([]);
  const [error, setError]       = useState(null);
  const [form, setForm]         = useState(null);   // null = closed, {} = new, {id,...} = editing
  const [saving, setSaving]     = useState(false);

  const load = () =>
    apiFetch('/api/rules')
      .then(setRules)
      .catch(e => setError(e.message));

  useEffect(() => { load(); }, []);

  const openNew  = () => setForm({ ...BLANK_RULE });
  const openEdit = (r) => setForm({ ...r, enabled: !!r.enabled });
  const closeForm = () => setForm(null);

  const handleSave = async () => {
    setSaving(true);
    try {
      const isEdit = form.id != null;
      const url    = isEdit ? `/api/rules/${form.id}` : '/api/rules';
      const method = isEdit ? 'PUT' : 'POST';
      const res    = await fetch(url, {
        method,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(form),
      });
      if (!res.ok) throw new Error(`${res.status}`);
      await load();
      closeForm();
    } catch (e) {
      setError(e.message);
    } finally {
      setSaving(false);
    }
  };

  const handleDelete = async (id) => {
    if (!confirm('Delete this rule?')) return;
    await fetch(`/api/rules/${id}`, { method: 'DELETE' });
    await load();
  };

  const handleToggle = async (id) => {
    await fetch(`/api/rules/${id}/toggle`, { method: 'PATCH' });
    await load();
  };

  const setField = (k) => (e) => {
    const val = e.target.type === 'checkbox' ? e.target.checked
              : e.target.type === 'number'   ? Number(e.target.value)
              : e.target.value;
    setForm(f => ({ ...f, [k]: val }));
  };

  return (
    <div>
      {error && <div className="error-banner">Error: {error}</div>}

      <div className="rules-header">
        <h2>Forwarding Rules</h2>
        <button className="btn btn-primary btn-sm" onClick={openNew}>+ New Rule</button>
      </div>

      {form && (
        <div className="rule-form">
          <div className="form-field" style={{ gridColumn: '1 / -1' }}>
            <label>Rule Name</label>
            <input value={form.name} onChange={setField('name')} placeholder="e.g. Emergency keyword" />
          </div>
          <div className="form-field">
            <label>Packet Type (blank = any)</label>
            <select value={form.packet_type || ''} onChange={setField('packet_type')}>
              <option value="">Any</option>
              {['text','telemetry','nodeinfo','position','traceroute','neighborinfo','map_report','unknown'].map(t =>
                <option key={t} value={t}>{t}</option>
              )}
            </select>
          </div>
          <div className="form-field">
            <label>From Node (blank = any)</label>
            <input value={form.from_node || ''} onChange={setField('from_node')} placeholder="!abcdef12" />
          </div>
          <div className="form-field">
            <label>Channel (blank = any)</label>
            <input value={form.channel || ''} onChange={setField('channel')} placeholder="index, e.g. 2" />
          </div>
          <div className="form-field">
            <label>Keyword (text only, blank = any)</label>
            <input value={form.keyword || ''} onChange={setField('keyword')} placeholder="emergency" />
          </div>
          <div className="form-field">
            <label>Pushover Priority</label>
            <select value={form.priority} onChange={setField('priority')}>
              <option value={-2}>-2 (no alert)</option>
              <option value={-1}>-1 (quiet)</option>
              <option value={0}>0 (normal)</option>
              <option value={1}>1 (high)</option>
              <option value={2}>2 (emergency)</option>
            </select>
          </div>
          <div className="form-field">
            <label>Sound</label>
            <input value={form.sound || 'pushover'} onChange={setField('sound')} placeholder="pushover" />
          </div>
          <div className="form-field" style={{ justifyContent: 'flex-end', paddingTop: '1.25rem' }}>
            <label style={{ display: 'flex', alignItems: 'center', gap: '0.4rem', cursor: 'pointer' }}>
              <input type="checkbox" checked={!!form.enabled} onChange={setField('enabled')} />
              Enabled
            </label>
          </div>
          <div className="form-actions">
            <button className="btn btn-primary btn-sm" onClick={handleSave} disabled={saving}>
              {saving ? 'Saving…' : (form.id ? 'Update' : 'Create')}
            </button>
            <button className="btn btn-ghost btn-sm" onClick={closeForm}>Cancel</button>
          </div>
        </div>
      )}

      {rules.length === 0
        ? <div className="empty">No rules — click "+ New Rule" to add one</div>
        : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>On</th>
                  <th>Name</th>
                  <th>Type</th>
                  <th>Node</th>
                  <th>Channel</th>
                  <th>Keyword</th>
                  <th>Priority</th>
                  <th>Sound</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {rules.map(r => (
                  <tr key={r.id}>
                    <td>
                      <input
                        type="checkbox"
                        className="toggle"
                        checked={!!r.enabled}
                        onChange={() => handleToggle(r.id)}
                      />
                    </td>
                    <td style={{ fontWeight: 500 }}>{r.name}</td>
                    <td>{r.packet_type ? typeBadge(r.packet_type) : <span className="muted">any</span>}</td>
                    <td className="mono muted">{r.from_node || <span>any</span>}</td>
                    <td className="muted">{r.channel || 'any'}</td>
                    <td className="muted">{r.keyword  || '—'}</td>
                    <td className="mono">{r.priority}</td>
                    <td className="muted">{r.sound}</td>
                    <td style={{ whiteSpace: 'nowrap' }}>
                      <button className="btn btn-ghost btn-sm" onClick={() => openEdit(r)} style={{ marginRight: 4 }}>Edit</button>
                      <button className="btn btn-danger btn-sm" onClick={() => handleDelete(r.id)}>Del</button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )
      }
    </div>
  );
}

// ── Graphs ──────────────────────────────────────────────────────────────────

// Metric metadata: label, unit, line color, and optional value formatter.
// Colors are CSS variables (not literal hex) so they stay legible against
// both the dark and light theme backgrounds — see :root / [data-theme=light]
// in style.css.
const METRICS = {
  temperature:         { label: 'Temperature',  unit: '°C',  color: 'var(--danger)' },
  relative_humidity:   { label: 'Humidity',     unit: '%',   color: 'var(--accent)' },
  barometric_pressure: { label: 'Pressure',     unit: 'hPa', color: 'var(--type-nodeinfo)' },
  battery_level:       { label: 'Battery',      unit: '%',   color: 'var(--success)' },
  voltage:             { label: 'Voltage',      unit: 'V',   color: 'var(--chart-yellow)' },
  channel_utilization: { label: 'Ch Util',      unit: '%',   color: 'var(--type-position)' },
  air_util_tx:         { label: 'Air Util TX',  unit: '%',   color: 'var(--type-text)' },
  iaq:                 { label: 'IAQ',          unit: '',    color: 'var(--type-neighborinfo)' },
  gas_resistance:      { label: 'Gas Res',      unit: 'Ω',   color: 'var(--success)' },
  rssi:                { label: 'RSSI',         unit: 'dBm', color: 'var(--danger)' },
  snr:                 { label: 'SNR',          unit: 'dB',  color: 'var(--type-text)' },
  hops_away:           { label: 'Hops',         unit: '',    color: 'var(--type-position)' },
};

// Preferred display order when a node has many metrics.
const METRIC_ORDER = [
  'temperature', 'relative_humidity', 'barometric_pressure', 'iaq', 'gas_resistance',
  'battery_level', 'voltage', 'channel_utilization', 'air_util_tx',
  'rssi', 'snr', 'hops_away',
];

// Dependency-free SVG line chart. data = [{t: isoString, v: number}, ...] oldest-first.
function LineChart({ data, color = 'var(--accent)', unit = '', height = 140 }) {
  const ref = useRef(null);
  const [w, setW] = useState(320);
  const [hover, setHover] = useState(null);

  // Track container width so the SVG is responsive.
  useEffect(() => {
    if (!ref.current) return;
    const update = () => setW(ref.current.clientWidth || 320);
    update();
    const ro = new ResizeObserver(update);
    ro.observe(ref.current);
    return () => ro.disconnect();
  }, []);

  const PAD_L = 40, PAD_R = 8, PAD_T = 8, PAD_B = 20;
  const innerW = Math.max(1, w - PAD_L - PAD_R);
  const innerH = Math.max(1, height - PAD_T - PAD_B);

  if (!data || data.length === 0) {
    return <div ref={ref} className="chart-empty">no data</div>;
  }

  const xs = data.map(d => { const p = parseTs(d.t); return p ? p.getTime() : NaN; });
  const ys = data.map(d => Number(d.v));
  const xMin = xs[0], xMax = xs[xs.length - 1];
  let yMin = Math.min(...ys), yMax = Math.max(...ys);
  if (yMin === yMax) { yMin -= 1; yMax += 1; }           // flat series → give it height
  const yPad = (yMax - yMin) * 0.08;
  yMin -= yPad; yMax += yPad;

  const sx = (t) => PAD_L + (xMax === xMin ? innerW / 2 : ((t - xMin) / (xMax - xMin)) * innerW);
  const sy = (v) => PAD_T + innerH - ((v - yMin) / (yMax - yMin)) * innerH;

  const path = data.map((d, i) => `${i === 0 ? 'M' : 'L'}${sx(xs[i]).toFixed(1)},${sy(ys[i]).toFixed(1)}`).join(' ');

  // Y-axis ticks (3).
  const yTicks = [yMin, (yMin + yMax) / 2, yMax].map(v => ({ v, y: sy(v) }));

  const onMove = (e) => {
    const rect = e.currentTarget.getBoundingClientRect();
    const mx = e.clientX - rect.left;
    // Nearest point by x.
    let best = 0, bestD = Infinity;
    for (let i = 0; i < xs.length; i++) {
      const d = Math.abs(sx(xs[i]) - mx);
      if (d < bestD) { bestD = d; best = i; }
    }
    setHover({ i: best, x: sx(xs[best]), y: sy(ys[best]) });
  };

  const fmtVal = (v) => (Math.abs(v) >= 1000 ? Math.round(v) : Math.round(v * 10) / 10);

  return (
    <div ref={ref} style={{ width: '100%' }}>
      <svg width={w} height={height} className="line-chart"
           onMouseMove={onMove} onMouseLeave={() => setHover(null)}>
        {yTicks.map((t, i) => (
          <g key={i}>
            <line x1={PAD_L} y1={t.y} x2={w - PAD_R} y2={t.y} className="grid" />
            <text x={PAD_L - 6} y={t.y + 3} textAnchor="end" className="axis-label">{fmtVal(t.v)}</text>
          </g>
        ))}
        <path d={path} fill="none" stroke={color} strokeWidth="1.75" />
        {hover && (
          <g>
            <line x1={hover.x} y1={PAD_T} x2={hover.x} y2={PAD_T + innerH} className="hover-line" />
            <circle cx={hover.x} cy={hover.y} r="3.5" fill={color} />
          </g>
        )}
      </svg>
      <div className="chart-hover-info">
        {hover
          ? <span><span className="mono">{fmtVal(ys[hover.i])}{unit}</span> · {fmtFull(data[hover.i].t)}</span>
          : <span className="muted">{data.length} pts · {fmtTs(data[0].t)} → {fmtTs(data[data.length - 1].t)}</span>}
      </div>
    </div>
  );
}

// min/avg/max/last/count for a fetched {t,v}[] series, rounded to 1 decimal.
function summarizeSeries(series) {
  if (!series || !series.length) return null;
  const vals = series.map(p => Number(p.v)).filter(v => !isNaN(v));
  if (!vals.length) return null;
  const round1 = v => Math.round(v * 10) / 10;
  return {
    count: vals.length,
    min:   round1(Math.min(...vals)),
    max:   round1(Math.max(...vals)),
    avg:   round1(vals.reduce((a, b) => a + b, 0) / vals.length),
    last:  round1(vals[vals.length - 1]),
  };
}

function downloadSeriesCsv(series, unit, filenameBase) {
  if (!series || !series.length) return;
  const header = ['timestamp', `value${unit ? ` (${unit})` : ''}`];
  const rows = series.map(p => [p.t, p.v]);
  const csv = [header, ...rows].map(r => r.join(',')).join('\n');
  const blob = new Blob([csv], { type: 'text/csv' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `${filenameBase}.csv`;
  a.click();
  URL.revokeObjectURL(url);
}

function MetricPanel({ node, metric, since, onEnlarge }) {
  const [data, setData] = useState(null);
  const tempUnit = React.useContext(TempUnitContext);
  const meta = METRICS[metric] || { label: metric, unit: '', color: 'var(--accent)' };

  useEffect(() => {
    let alive = true;
    setData(null);
    apiFetch(`/api/telemetry/series?node=${encodeURIComponent(node)}&metric=${metric}&since=${since}`)
      .then(d => { if (alive) setData(d); })
      .catch(() => { if (alive) setData([]); });
    return () => { alive = false; };
  }, [node, metric, since]);

  const { series, unit } = applyUnit(metric, data, meta.unit, tempUnit);
  const last = series && series.length ? series[series.length - 1].v : null;

  return (
    <div
      className={`metric-panel${onEnlarge ? ' metric-panel-clickable' : ''}`}
      onClick={onEnlarge}
      role={onEnlarge ? 'button' : undefined}
      tabIndex={onEnlarge ? 0 : undefined}
      onKeyDown={onEnlarge ? (e => { if (e.key === 'Enter') onEnlarge(); }) : undefined}
      title={onEnlarge ? 'Click to enlarge' : undefined}
    >
      <div className="metric-head">
        <span className="metric-title" style={{ color: meta.color }}>{meta.label}</span>
        <span className="metric-head-right">
          {last !== null && (
            <span className="metric-current mono">
              {Math.round(last * 10) / 10}{unit}
            </span>
          )}
          {onEnlarge && <span className="expand-icon">⤢</span>}
        </span>
      </div>
      {data === null
        ? <div className="chart-empty">loading…</div>
        : <LineChart data={series} color={meta.color} unit={unit} />}
    </div>
  );
}

// Enlarged, single-metric chart with its own range control, summary stats,
// and CSV export — opened by clicking a MetricPanel for deeper analysis.
function ChartModal({ node, nodeLabel, metric, since: initialSince, onClose }) {
  const [since, setSince] = useState(initialSince);
  const [data, setData] = useState(null);
  const tempUnit = React.useContext(TempUnitContext);
  const meta = METRICS[metric] || { label: metric, unit: '', color: 'var(--accent)' };

  useEffect(() => {
    let alive = true;
    setData(null);
    apiFetch(`/api/telemetry/series?node=${encodeURIComponent(node)}&metric=${metric}&since=${since}`)
      .then(d => { if (alive) setData(d); })
      .catch(() => { if (alive) setData([]); });
    return () => { alive = false; };
  }, [node, metric, since]);

  useEffect(() => {
    const onKey = e => { if (e.key === 'Escape') onClose(); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  const { series, unit } = applyUnit(metric, data, meta.unit, tempUnit);
  const stats = summarizeSeries(series);

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal-panel modal-panel-wide" onClick={e => e.stopPropagation()}>
        <div className="modal-header">
          <h3><span style={{ color: meta.color }}>{meta.label}</span> · {nodeLabel}</h3>
          <button className="link-btn" onClick={onClose}>✕</button>
        </div>

        <div className="chart-modal-controls">
          <div className="filter-group">
            <label>Range</label>
            <select value={since} onChange={e => setSince(e.target.value)}>
              <option value="6h">Last 6h</option>
              <option value="24h">Last 24h</option>
              <option value="7d">Last 7d</option>
              <option value="30d">Last 30d</option>
              <option value="all">All</option>
            </select>
          </div>
          <button
            className="btn btn-ghost btn-sm"
            disabled={!series || !series.length}
            onClick={() => downloadSeriesCsv(series, unit, `${node}-${metric}-${since}`)}
          >
            Export CSV
          </button>
        </div>

        {data === null
          ? <div className="chart-empty">loading…</div>
          : <LineChart data={series} color={meta.color} unit={unit} height={360} />}

        {stats && (
          <div className="chart-modal-stats mono">
            <div><span className="dt-label">Min</span>{stats.min}{unit}</div>
            <div><span className="dt-label">Avg</span>{stats.avg}{unit}</div>
            <div><span className="dt-label">Max</span>{stats.max}{unit}</div>
            <div><span className="dt-label">Last</span>{stats.last}{unit}</div>
            <div><span className="dt-label">Points</span>{stats.count}</div>
          </div>
        )}
      </div>
    </div>
  );
}

function Graphs() {
  const [nodes, setNodes]   = useState([]);
  const [node, setNode]     = usePersistentState('graphs.node', '');
  const [metrics, setMetrics] = useState(null);
  const [since, setSince]   = usePersistentState('graphs.since', '24h');
  const [localOnly, setLocalOnly] = usePersistentState('graphs.localOnly', false);
  const [enlarged, setEnlarged] = useState(null); // metric name, or null

  // Load node list once; default to the most-recently-seen node.
  useEffect(() => {
    apiFetch('/api/nodes')
      .then(ns => {
        setNodes(ns);
        if (ns.length && !node) setNode(ns[0].node_id);
      })
      .catch(() => {});
  }, []);

  // "Local only" = direct RF neighbors (last packet arrived 0 hops away),
  // which filters out distant/relayed junk nodes. Matches the Nodes tab.
  const nodeList = localOnly ? nodes.filter(n => n.last_hops === 0) : nodes;

  // If the currently-selected node falls outside the filter, jump to the first
  // visible one so the charts don't show a node that's been filtered away.
  useEffect(() => {
    if (!nodeList.length) return;
    if (!nodeList.some(n => n.node_id === node)) setNode(nodeList[0].node_id);
  }, [localOnly, nodes]);

  // When the node changes, find which metrics it actually has.
  useEffect(() => {
    if (!node) return;
    setMetrics(null);
    apiFetch(`/api/telemetry/metrics?node=${encodeURIComponent(node)}`)
      .then(m => setMetrics(m))
      .catch(() => setMetrics([]));
  }, [node]);

  const nodeLabel = (n) => {
    const name = nodeDisplayName(n);
    return name === n.node_id ? n.node_id : `${name} (${n.node_id})`;
  };

  const ordered = metrics
    ? METRIC_ORDER.filter(m => metrics.includes(m))
    : [];

  return (
    <div>
      <div className="filter-bar">
        <div className="filter-group">
          <label>Node</label>
          <select value={node} onChange={e => setNode(e.target.value)}>
            {nodeList.length === 0 && <option value="">{localOnly ? 'No direct nodes' : 'No nodes yet'}</option>}
            {nodeList.map(n => (
              <option key={n.node_id} value={n.node_id}>{nodeLabel(n)}</option>
            ))}
          </select>
        </div>
        <div className="filter-group">
          <label>Range</label>
          <select value={since} onChange={e => setSince(e.target.value)}>
            <option value="6h">Last 6h</option>
            <option value="24h">Last 24h</option>
            <option value="7d">Last 7d</option>
            <option value="30d">Last 30d</option>
            <option value="all">All</option>
          </select>
        </div>
        <label className="hop-toggle">
          <input type="checkbox" checked={localOnly} onChange={e => setLocalOnly(e.target.checked)} />
          Local nodes only (0 hops)
        </label>
      </div>

      {nodeList.length === 0
        ? <div className="empty">{localOnly ? 'No direct (0-hop) nodes heard.' : 'No nodes have reported yet.'}</div>
        : !node
        ? <div className="empty">No nodes have reported yet.</div>
        : metrics === null
          ? <div className="empty">Loading…</div>
          : ordered.length === 0
            ? <div className="empty">No telemetry or signal data recorded for this node.</div>
            : (
              <div className="metric-grid">
                {ordered.map(m => (
                  <MetricPanel key={m} node={node} metric={m} since={since} onEnlarge={() => setEnlarged(m)} />
                ))}
              </div>
            )}

      {enlarged && (
        <ChartModal
          node={node}
          nodeLabel={nodeLabel((nodeList.find(n => n.node_id === node)) || { node_id: node })}
          metric={enlarged}
          since={since}
          onClose={() => setEnlarged(null)}
        />
      )}
    </div>
  );
}

// ── Report builder ──────────────────────────────────────────────────────────

const REPORT_METRICS = ['temperature', 'relative_humidity', 'barometric_pressure',
                        'battery_level', 'voltage', 'iaq', 'rssi', 'snr'];

function Report() {
  const tempUnit = React.useContext(TempUnitContext);
  const [nodes, setNodes]       = useState([]);
  const [selNodes, setSelNodes] = usePersistentState('report.nodes', []);     // node_id[]
  const [selMetrics, setSelMetrics] = usePersistentState('report.metrics', ['temperature', 'rssi']);
  const [since, setSince]       = usePersistentState('report.since', '24h');
  const [report, setReport]     = useState(null);
  const [loading, setLoading]   = useState(false);
  const [error, setError]       = useState(null);
  const [configOpen, setConfigOpen] = usePersistentState('report.configOpen', false);

  useEffect(() => {
    apiFetch('/api/nodes').then(setNodes).catch(() => {});
  }, []);

  const toggle = (arr, set, val) =>
    set(arr.includes(val) ? arr.filter(x => x !== val) : [...arr, val]);

  const generate = () => {
    if (!selNodes.length || !selMetrics.length) {
      setError('Pick at least one node and one metric.');
      return;
    }
    setError(null);
    setLoading(true);
    const p = new URLSearchParams({
      nodes: selNodes.join(','),
      metrics: selMetrics.join(','),
      since,
    });
    apiFetch(`/api/report?${p}`)
      .then(setReport)
      .catch(e => setError(e.message))
      .finally(() => setLoading(false));
  };

  const exportJson = () => {
    if (!report) return;
    const blob = new Blob([JSON.stringify(report, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `mesh-report-${new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-')}.json`;
    a.click();
    URL.revokeObjectURL(url);
  };

  return (
    <div>
      <div className="report-controls no-print collapsible-section">
        <button className="collapsible-header" onClick={() => setConfigOpen(o => !o)}>
          <span className={`collapse-caret ${configOpen ? 'open' : ''}`}>▶</span>
          <span>Report Configuration</span>
          <span className="muted collapsible-summary">
            {selNodes.length} node{selNodes.length === 1 ? '' : 's'}, {selMetrics.length} metric{selMetrics.length === 1 ? '' : 's'}, {since}
          </span>
        </button>
        {configOpen && (
          <div className="collapsible-body">
            <div className="report-pick">
              <div className="report-pick-label">Nodes</div>
              <div className="chip-row">
                {nodes.length === 0 && <span className="muted">No nodes yet</span>}
                {nodes.map(n => (
                  <button
                    key={n.node_id}
                    className={`chip ${selNodes.includes(n.node_id) ? 'on' : ''}`}
                    onClick={() => toggle(selNodes, setSelNodes, n.node_id)}
                  >
                    {nodeDisplayName(n)}
                  </button>
                ))}
              </div>
            </div>

            <div className="report-pick">
              <div className="report-pick-label">Metrics</div>
              <div className="chip-row">
                {REPORT_METRICS.map(m => (
                  <button
                    key={m}
                    className={`chip ${selMetrics.includes(m) ? 'on' : ''}`}
                    onClick={() => toggle(selMetrics, setSelMetrics, m)}
                  >
                    {(METRICS[m] || {}).label || m}
                  </button>
                ))}
              </div>
            </div>

            <div className="report-actions">
              <div className="filter-group">
                <label>Range</label>
                <select value={since} onChange={e => setSince(e.target.value)}>
                  <option value="6h">6h</option>
                  <option value="24h">24h</option>
                  <option value="7d">7d</option>
                  <option value="30d">30d</option>
                  <option value="all">All</option>
                </select>
              </div>
              <button className="btn-primary" onClick={generate} disabled={loading}>
                {loading ? 'Generating…' : 'Generate report'}
              </button>
              {report && <button className="btn-ghost" onClick={exportJson}>Export JSON</button>}
              {report && <button className="btn-ghost" onClick={() => window.print()}>Print</button>}
            </div>
          </div>
        )}
      </div>

      {error && <div className="error-banner">Error: {error}</div>}

      {report && (
        <div className="report-output">
          <div className="report-header">
            <h2>Mesh Report</h2>
            <span className="muted">
              Range: {report.since} · Generated {fmtFull(report.generated_at)}
            </span>
          </div>

          {report.nodes.map(node => (
            <div key={node.node_id} className="report-node">
              <h3>{node.name} <span className="mono muted">{node.node_id}</span></h3>
              <div className="metric-grid">
                {selMetrics.map(m => {
                  const md = node.metrics[m];
                  const meta = METRICS[m] || { label: m, unit: '', color: 'var(--accent)' };
                  // Convert temperature summary + series to the active unit.
                  const conv = (v) => m === 'temperature' ? Math.round(convertTemp(v, tempUnit) * 10) / 10 : v;
                  const { series, unit } = applyUnit(m, md && md.series, meta.unit, tempUnit);
                  return (
                    <div key={m} className="metric-panel">
                      <div className="metric-head">
                        <span className="metric-title" style={{ color: meta.color }}>{meta.label}</span>
                        {md && md.summary
                          ? <span className="metric-current mono">{conv(md.summary.last)}{unit}</span>
                          : <span className="muted">no data</span>}
                      </div>
                      {md && md.summary && (
                        <div className="report-stats mono">
                          min {conv(md.summary.min)}{unit} · avg {conv(md.summary.avg)}{unit} · max {conv(md.summary.max)}{unit} · n={md.summary.count}
                        </div>
                      )}
                      {md && md.series && md.series.length > 0 &&
                        <LineChart data={series} color={meta.color} unit={unit} />}
                    </div>
                  );
                })}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// ── Weather ───────────────────────────────────────────────────────────────────
// Off-grid forecasting from locally stored node telemetry only — no internet,
// no NWP model. See weather_forecast.py for the actual pressure-tendency
// nowcast / climatology-outlook / diurnal-temperature-climatology logic; this
// is purely presentation.

const WEATHER_METRICS = ['temperature', 'relative_humidity', 'barometric_pressure', 'iaq', 'gas_resistance'];
const OUTLOOK_LEVELS = {
  low:      { label: 'Low',      color: 'var(--success)' },
  moderate: { label: 'Moderate', color: 'var(--type-position)' },
  elevated: { label: 'Elevated', color: 'var(--danger)' },
  unknown:  { label: '—',        color: 'var(--muted)' },
};
const TEMP_RANGE_HOURS = { '24h': 24, '3d': 72, '7d': 168 };

function WeatherConfig({ nodes, config, onSaved }) {
  const [local, setLocal] = useState(config);
  const [saving, setSaving] = useState(false);
  const [open, setOpen] = usePersistentState('weather.configOpen', false);

  useEffect(() => { setLocal(config); }, [config]);

  const toggle = (metric, nodeId) => {
    setLocal(prev => {
      const cur = prev[metric] || [];
      const next = cur.includes(nodeId) ? cur.filter(n => n !== nodeId) : [...cur, nodeId];
      return { ...prev, [metric]: next };
    });
  };

  const save = () => {
    setSaving(true);
    Promise.all(
      WEATHER_METRICS.map(m =>
        apiFetch('/api/weather/config', {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ metric: m, node_ids: local[m] || [] }),
        })
      )
    ).then(onSaved).finally(() => setSaving(false));
  };

  const configuredCount = WEATHER_METRICS.reduce((n, m) => n + ((config[m] || []).length > 0 ? 1 : 0), 0);

  return (
    <div className="report-controls no-print collapsible-section">
      <button className="collapsible-header" onClick={() => setOpen(o => !o)}>
        <span className={`collapse-caret ${open ? 'open' : ''}`}>▶</span>
        <span>Weather Station Configuration</span>
        <span className="muted collapsible-summary">
          {configuredCount}/{WEATHER_METRICS.length} metrics configured
        </span>
      </button>
      {open && (
        <div className="collapsible-body">
          {WEATHER_METRICS.map(m => {
            const meta = METRICS[m] || { label: m, color: 'var(--accent)' };
            return (
              <div key={m} className="report-pick">
                <div className="report-pick-label" style={{ color: meta.color }}>{meta.label}</div>
                <div className="chip-row">
                  {nodes.length === 0 && <span className="muted">No nodes yet</span>}
                  {nodes.map(n => (
                    <button
                      key={n.node_id}
                      className={`chip ${(local[m] || []).includes(n.node_id) ? 'on' : ''}`}
                      onClick={() => toggle(m, n.node_id)}
                    >
                      {nodeDisplayName(n)}
                    </button>
                  ))}
                </div>
              </div>
            );
          })}
          <div className="report-actions">
            <button className="btn btn-primary" onClick={save} disabled={saving}>
              {saving ? 'Saving…' : 'Save Configuration'}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

function WeatherOutlook({ outlook }) {
  if (!outlook.available) {
    return <div className="empty">{outlook.reason}</div>;
  }
  return (
    <div>
      <div className="forecast-day-grid">
        {outlook.days.map(d => {
          const lvl = OUTLOOK_LEVELS[d.level] || OUTLOOK_LEVELS.unknown;
          return (
            <div key={d.day_offset} className="forecast-day-card">
              <div className="forecast-day-label">Day +{d.day_offset}</div>
              <div className="forecast-day-badge" style={{ color: lvl.color, borderColor: lvl.color }}>
                {lvl.label}
              </div>
            </div>
          );
        })}
      </div>
      <div className="muted forecast-outlook-note">
        Outlook derived from {outlook.lookback_days}d of local pressure history — a climatological
        rain-signature frequency, not a precise forecast. Days 1–2 (nowcast above) use live pressure trend instead.
      </div>
    </div>
  );
}

function Weather() {
  const tempUnit = React.useContext(TempUnitContext);
  const [nodes, setNodes]     = useState([]);
  const [forecast, setForecast] = useState(null);
  const [error, setError]     = useState(null);
  const [tempRange, setTempRange] = usePersistentState('weather.tempRange', '3d');

  const load = () =>
    apiFetch('/api/weather/forecast')
      .then(d => { setForecast(d); setError(null); })
      .catch(e => setError(e.message));

  useEffect(() => {
    apiFetch('/api/nodes').then(setNodes).catch(() => {});
    load();
  }, []);

  if (!forecast && !error) return <div className="empty">Loading…</div>;

  const cfg     = forecast ? forecast.config : {};
  const current = forecast ? forecast.current : {};
  const nowcast = forecast ? forecast.nowcast : { available: false };
  const outlook = forecast ? forecast.outlook : { available: false, days: [] };
  const hourly  = forecast ? forecast.hourly_temperature : { available: false, series: [] };

  const hoursToShow = TEMP_RANGE_HOURS[tempRange] || 72;
  const tempSeriesRaw = (hourly.series || []).slice(0, hoursToShow);
  const { series: tempSeries, unit: tempSeriesUnit } = applyUnit('temperature', tempSeriesRaw, '°C', tempUnit);

  return (
    <div>
      {error && <div className="error-banner">Error: {error}</div>}

      <WeatherConfig nodes={nodes} config={cfg} onSaved={load} />

      <h3 className="section-label">Current Conditions</h3>
      <div className="stats-grid">
        {WEATHER_METRICS.map(m => {
          const meta = METRICS[m] || { label: m, unit: '', color: 'var(--accent)' };
          const reading = current[m];
          const assigned = (cfg[m] || []).length > 0;
          const displayVal = reading
            ? (m === 'temperature' ? fmtTemp(reading.value, tempUnit) : `${reading.value}${meta.unit}`)
            : null;
          return (
            <div key={m} className="stat-card">
              <div className="value" style={{ color: meta.color, fontSize: displayVal ? '1.75rem' : '0.8rem' }}>
                {displayVal || (assigned ? 'no data' : 'not configured')}
              </div>
              <div className="label">{meta.label}</div>
            </div>
          );
        })}
      </div>

      <h3 className="section-label">24–48h Nowcast</h3>
      {!nowcast.available
        ? <div className="empty">{nowcast.reason}</div>
        : (
          <div className="metric-panel">
            <div className="nowcast-summary">{nowcast.summary}</div>
            <div className="node-detail-grid" style={{ marginTop: '0.5rem' }}>
              <div><span className="dt-label">Pressure</span><span className="mono">{nowcast.pressure_hpa} hPa</span></div>
              <div>
                <span className="dt-label">3h Trend</span>
                <span className="mono">
                  {nowcast.trend_3h_hpa != null ? `${nowcast.trend_3h_hpa > 0 ? '+' : ''}${nowcast.trend_3h_hpa} hPa` : '—'} ({nowcast.trend_label})
                </span>
              </div>
              <div><span className="dt-label">Rain chance (24h)</span><span className="mono">{nowcast.rain_chance_24h}%</span></div>
              <div><span className="dt-label">Rain chance (48h)</span><span className="mono">{nowcast.rain_chance_48h}%</span></div>
            </div>
          </div>
        )}

      <h3 className="section-label">3–7 Day Rain Outlook</h3>
      <WeatherOutlook outlook={outlook} />

      <h3 className="section-label">Hourly Temperature</h3>
      <div className="filter-bar" style={{ marginBottom: '0.75rem' }}>
        <div className="filter-group">
          <label>Range</label>
          <select value={tempRange} onChange={e => setTempRange(e.target.value)}>
            <option value="24h">Next 24h</option>
            <option value="3d">Next 3 days</option>
            <option value="7d">Next 7 days</option>
          </select>
        </div>
      </div>
      {!hourly.available
        ? <div className="empty">{hourly.reason}</div>
        : (
          <div className="metric-panel">
            <LineChart data={tempSeries} color={(METRICS.temperature || {}).color} unit={tempSeriesUnit} height={260} />
          </div>
        )}
    </div>
  );
}

// ── Stats ─────────────────────────────────────────────────────────────────────

function Stats() {
  const [stats, setStats]   = useState(null);
  const [error, setError]   = useState(null);

  const load = () =>
    apiFetch('/api/stats')
      .then(d => { setStats(d); setError(null); })
      .catch(e => setError(e.message));

  useEffect(() => {
    load();
    const id = setInterval(load, 30000);
    return () => clearInterval(id);
  }, []);

  if (!stats && !error) return <div className="empty">Loading…</div>;

  return (
    <div>
      {error && <div className="error-banner">Error: {error}</div>}
      {stats && (
        <>
          <div className="stats-grid">
            <div className="stat-card">
              <div className="value">{stats.packets_1h}</div>
              <div className="label">Packets (1h)</div>
            </div>
            <div className="stat-card">
              <div className="value">{stats.packets_24h}</div>
              <div className="label">Packets (24h)</div>
            </div>
            <div className="stat-card">
              <div className="value">{stats.active_nodes_24h}</div>
              <div className="label">Active Nodes (24h)</div>
            </div>
            <div className="stat-card">
              <div className="value" style={{ fontSize: '1rem', paddingTop: '0.25rem' }}>
                {stats.last_packet_at ? fmtTs(stats.last_packet_at) : '—'}
              </div>
              <div className="label">Last Packet</div>
            </div>
          </div>

          <h3 style={{ fontSize: '0.875rem', fontWeight: 600, color: 'var(--muted)', marginBottom: '0.5rem', letterSpacing: '0.04em', textTransform: 'uppercase' }}>
            Packets by Type (24h)
          </h3>
          {stats.by_type_24h.length === 0
            ? <div className="empty">No packets in the last 24h</div>
            : (
              <div className="by-type-list" style={{ maxWidth: 420 }}>
                {stats.by_type_24h.map(row => (
                  <div key={row.packet_type} className="by-type-row">
                    {typeBadge(row.packet_type)}
                    <span className="count mono">{row.count}</span>
                  </div>
                ))}
              </div>
            )
          }
        </>
      )}
    </div>
  );
}

// ── Maintenance ───────────────────────────────────────────────────────────────

function formatBytes(bytes) {
  if (bytes == null) return '—';
  if (bytes < 1024) return `${bytes} B`;
  const units = ['KB', 'MB', 'GB', 'TB'];
  let val = bytes, i = -1;
  do { val /= 1024; i++; } while (val >= 1024 && i < units.length - 1);
  return `${val.toFixed(1)} ${units[i]}`;
}

function Maintenance() {
  const [stats, setStats]   = useState(null);
  const [config, setConfig] = useState(null);
  const [error, setError]   = useState(null);
  const [days, setDays]     = useState(90);
  const [vacuum, setVacuum] = useState(true);
  const [busy, setBusy]     = useState(false);
  const [result, setResult] = useState(null);

  const loadStats  = () => apiFetch('/api/maintenance/stats').then(setStats).catch(e => setError(e.message));
  const loadConfig = () => apiFetch('/api/maintenance/config').then(c => { setConfig(c); setDays(c.retention_days); }).catch(() => {});

  useEffect(() => { loadStats(); loadConfig(); }, []);

  const runCleanup = () => {
    if (!window.confirm(`Permanently delete packets and messages older than ${days} day(s)? This cannot be undone.`)) return;
    setBusy(true);
    setResult(null);
    apiFetch('/api/maintenance/cleanup', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ retention_days: days, vacuum }),
    })
      .then(r => { setResult(r); loadStats(); })
      .catch(e => setError(e.message))
      .finally(() => setBusy(false));
  };

  const saveConfig = () => {
    apiFetch('/api/maintenance/config', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(config),
    }).then(setConfig).catch(e => setError(e.message));
  };

  if (!stats && !error) return <div className="empty">Loading…</div>;

  const totalRows  = stats ? stats.tables.reduce((a, t) => a + t.rows, 0) : 0;
  const totalBytes = stats ? (stats.main_db_bytes || 0) + (stats.bot_db_bytes || 0) : 0;

  return (
    <div>
      {error && <div className="error-banner">Error: {error}</div>}

      <h3 className="section-label">Storage Overview</h3>
      <div className="stats-grid">
        <div className="stat-card">
          <div className="value">{formatBytes(totalBytes)}</div>
          <div className="label">Total DB Size</div>
        </div>
        <div className="stat-card">
          <div className="value">{totalRows.toLocaleString()}</div>
          <div className="label">Total Rows</div>
        </div>
        <div className="stat-card">
          <div className="value" style={{ fontSize: '1rem', paddingTop: '0.25rem' }}>
            {stats && stats.oldest_packet_at ? fmtTs(stats.oldest_packet_at) : '—'}
          </div>
          <div className="label">Oldest Packet</div>
        </div>
        <div className="stat-card">
          <div className="value" style={{ fontSize: '1rem', paddingTop: '0.25rem' }}>
            {stats && stats.newest_packet_at ? fmtTs(stats.newest_packet_at) : '—'}
          </div>
          <div className="label">Newest Packet</div>
        </div>
      </div>

      <h3 className="section-label">Tables</h3>
      <div className="by-type-list" style={{ maxWidth: 420 }}>
        {stats && stats.tables.map(t => (
          <div key={`${t.db}.${t.table}`} className="by-type-row">
            <span className="mono">{t.table}</span>
            <span className="muted" style={{ marginLeft: '0.35rem' }}>({t.db})</span>
            <span className="count mono">{t.rows.toLocaleString()}</span>
          </div>
        ))}
      </div>

      <h3 className="section-label">Manual Cleanup</h3>
      <div className="filter-bar no-print">
        <div className="filter-group">
          <label>Delete data older than</label>
          <input type="number" min="1" value={days} onChange={e => setDays(parseInt(e.target.value, 10) || 1)} />
          <label>days</label>
        </div>
        <label className="hop-toggle">
          <input type="checkbox" checked={vacuum} onChange={e => setVacuum(e.target.checked)} />
          Reclaim disk space (VACUUM)
        </label>
        <button className="btn btn-primary" onClick={runCleanup} disabled={busy}>
          {busy ? 'Cleaning…' : 'Clean Now'}
        </button>
      </div>
      {result && (
        <div className="muted" style={{ marginTop: '-0.5rem', marginBottom: '0.75rem' }}>
          Deleted {result.packets_deleted.toLocaleString()} packets and {result.text_messages_deleted.toLocaleString()} messages older than {result.cutoff}.
        </div>
      )}

      {config && (
        <>
          <h3 className="section-label">Auto Cleanup</h3>
          <div className="filter-bar no-print">
            <label className="hop-toggle">
              <input type="checkbox" checked={config.enabled} onChange={e => setConfig({ ...config, enabled: e.target.checked })} />
              Enabled
            </label>
            <div className="filter-group">
              <label>Retention (days)</label>
              <input type="number" min="1" value={config.retention_days}
                     onChange={e => setConfig({ ...config, retention_days: parseInt(e.target.value, 10) || 1 })} />
            </div>
            <div className="filter-group">
              <label>Run at hour (UTC)</label>
              <input type="number" min="0" max="23" value={config.run_hour}
                     onChange={e => setConfig({ ...config, run_hour: parseInt(e.target.value, 10) || 0 })} />
            </div>
            <button className="btn btn-primary" onClick={saveConfig}>Save</button>
          </div>
        </>
      )}
    </div>
  );
}

// ── Map ───────────────────────────────────────────────────────────────────────

// Stable per-node colors for trail lines
const NODE_COLORS = ['#58a6ff','#ffa657','#56d364','#bc8cff','#f85149','#79c0ff','#d2a8ff','#ffd700','#ff7f7f','#7ee8a2'];
function nodeColor(node_id) {
  let h = 0;
  for (let i = 0; i < node_id.length; i++) h = (h * 31 + node_id.charCodeAt(i)) >>> 0;
  return NODE_COLORS[h % NODE_COLORS.length];
}

const TRAIL_WINDOWS = ['1h','6h','24h','7d','all'];

// Timeframe options for the latest-position markers: how recently a node's
// last position must have been heard to show on the map.
const MAP_TIMEFRAMES = [
  { label: '24 hours', value: '24h', ms: 24 * 3600 * 1000 },
  { label: '3 days',   value: '3d',  ms: 3 * 24 * 3600 * 1000 },
  { label: '7 days',   value: '7d',  ms: 7 * 24 * 3600 * 1000 },
  { label: 'All',      value: 'all', ms: null },
];

function NodeMap() {
  const tempUnit     = React.useContext(TempUnitContext);
  const mapRef       = useRef(null);
  const leafletRef   = useRef(null);
  const markersRef   = useRef({});   // node_id -> labeled marker (latest position)
  const trailsRef    = useRef({});   // node_id -> { polyline, dots: [] }
  const hasZoomedRef = useRef(false);

  const [nodes,       setNodes]       = useState([]);   // latest-per-node from /api/map
  const [trailData,   setTrailData]   = useState(null); // array from /api/map/trail, null = off
  const [trailWindow, setTrailWindow] = usePersistentState('map.trailWindow', '24h');
  const [showTrails,  setShowTrails]  = usePersistentState('map.showTrails', false);
  const [localOnly,   setLocalOnly]   = usePersistentState('map.localOnly', false);
  const [timeframe,   setTimeframe]   = usePersistentState('map.timeframe', '24h');
  const [error,       setError]       = useState(null);
  const [loading,     setLoading]     = useState(true);
  const [trailLoading,setTrailLoading]= useState(false);

  // Init map once
  useEffect(() => {
    if (leafletRef.current) return;
    leafletRef.current = L.map(mapRef.current, { zoomControl: true }).setView([39.5, -98.35], 4);
    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
      attribution: '© OpenStreetMap contributors',
      maxZoom: 19,
    }).addTo(leafletRef.current);
    return () => {
      if (leafletRef.current) { leafletRef.current.remove(); leafletRef.current = null; }
    };
  }, []);

  // Load latest-per-node positions
  const load = useCallback(() => {
    apiFetch('/api/map')
      .then(d => { setNodes(d); setError(null); })
      .catch(e => setError(e.message))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => { load(); }, [load]);
  useEffect(() => {
    const id = setInterval(load, 30000);
    return () => clearInterval(id);
  }, [load]);

  // Load trail data when trails are toggled on or window changes
  const loadTrails = useCallback(() => {
    if (!showTrails) return;
    setTrailLoading(true);
    apiFetch(`/api/map/trail?since=${trailWindow}`)
      .then(d => setTrailData(d))
      .catch(e => setError(e.message))
      .finally(() => setTrailLoading(false));
  }, [showTrails, trailWindow]);

  useEffect(() => {
    if (showTrails) loadTrails();
    else setTrailData(null);
  }, [showTrails, trailWindow, loadTrails]);

  // Draw / clear trail layers
  useEffect(() => {
    const map = leafletRef.current;
    if (!map) return;

    // Remove all existing trail layers
    Object.values(trailsRef.current).forEach(({ polyline, dots }) => {
      polyline.remove();
      dots.forEach(d => d.remove());
    });
    trailsRef.current = {};

    if (!trailData) return;

    trailData.forEach(node => {
      if (!node.points || node.points.length === 0) return;
      const color  = nodeColor(node.node_id);
      const latlngs = node.points.map(p => [p.lat, p.lon]);

      const polyline = L.polyline(latlngs, { color, weight: 2, opacity: 0.6 }).addTo(map);

      const dots = node.points.map((p, i) => {
        const isLast = i === node.points.length - 1;
        const dot = L.circleMarker([p.lat, p.lon], {
          radius: isLast ? 0 : 4,  // latest pos handled by labeled marker
          color,
          fillColor: color,
          fillOpacity: 0.7,
          weight: 1,
        });
        const popupLines = [
          `<strong>${node.long_name || node.node_id}</strong>`,
          `<span style="color:#8b949e">${fmtTs(p.received_at)}</span>`,
          p.altitude  != null ? `Alt: ${p.altitude}m` : null,
          p.rssi      != null ? `RSSI ${p.rssi}`       : null,
          p.snr       != null ? `SNR ${p.snr}`          : null,
        ].filter(Boolean).join('<br>');
        dot.bindPopup(popupLines).addTo(map);
        return dot;
      });

      trailsRef.current[node.node_id] = { polyline, dots };
    });
  }, [trailData]);

  // Apply the "local nodes" (0-hop direct neighbors) and timeframe filters to
  // the latest-per-node positions. Matches the "local only" pattern on the
  // Nodes and Graphs tabs; timeframe filters by how recently the last position
  // was heard.
  const cutoffMs = (MAP_TIMEFRAMES.find(t => t.value === timeframe) || {}).ms;
  const visibleNodes = nodes.filter(n => {
    if (localOnly && n.hops_away !== 0) return false;
    if (cutoffMs != null) {
      const d = parseTs(n.received_at);
      if (!d || Date.now() - d.getTime() > cutoffMs) return false;
    }
    return true;
  });

  // Update labeled markers when the visible (filtered) node set changes
  useEffect(() => {
    const map = leafletRef.current;
    if (!map) return;

    map.invalidateSize();

    const seen = new Set();
    visibleNodes.forEach(n => {
      seen.add(n.node_id);
      const label = n.short_name || n.long_name || n.node_id;
      const color = nodeColor(n.node_id);
      const lines = [
        `<strong>${n.long_name || n.node_id}</strong>`,
        `<span style="color:#8b949e">${n.node_id}</span>`,
        n.temperature != null ? `🌡 ${fmtTemp(n.temperature, tempUnit)}` : null,
        n.altitude    != null ? `Alt: ${n.altitude}m`   : null,
        n.rssi        != null ? `RSSI ${n.rssi}`         : null,
        n.snr         != null ? `SNR ${n.snr}`           : null,
        n.hops_away   != null ? `${n.hops_away} hop${n.hops_away !== 1 ? 's' : ''}` : null,
        `<span style="color:#8b949e">${fmtTs(n.received_at)}</span>`,
      ].filter(Boolean).join('<br>');

      const icon = L.divIcon({
        className: '',
        html: `<div class="map-marker" style="background:${color}">${label}</div>`,
        iconAnchor: [0, 0],
      });

      if (markersRef.current[n.node_id]) {
        markersRef.current[n.node_id]
          .setLatLng([n.lat, n.lon])
          .setIcon(icon)
          .getPopup().setContent(lines);
      } else {
        markersRef.current[n.node_id] = L.marker([n.lat, n.lon], { icon })
          .bindPopup(lines)
          .addTo(map);
      }
    });

    Object.keys(markersRef.current).forEach(id => {
      if (!seen.has(id)) { markersRef.current[id].remove(); delete markersRef.current[id]; }
    });

    if (!hasZoomedRef.current && visibleNodes.length > 0) {
      hasZoomedRef.current = true;
      const latlngs = visibleNodes.map(n => [n.lat, n.lon]);
      try { map.fitBounds(latlngs, { padding: [40, 40], maxZoom: 14 }); } catch (_) {}
    }
  }, [nodes, localOnly, timeframe, tempUnit]);

  const totalPoints = trailData ? trailData.reduce((s, n) => s + n.points.length, 0) : 0;

  return (
    <div>
      {error && <div className="error-banner">Error: {error}</div>}
      <div style={{ display: 'flex', alignItems: 'center', gap: '0.75rem', marginBottom: '0.625rem', flexWrap: 'wrap' }}>
        <span style={{ fontSize: '0.8125rem', color: 'var(--muted)' }}>
          {loading
            ? 'Loading…'
            : `${visibleNodes.length}${visibleNodes.length !== nodes.length ? ` of ${nodes.length}` : ''} node${visibleNodes.length !== 1 ? 's' : ''} with position data`}
        </span>
        <span className="auto-badge">auto-refresh 30s</span>
        <div className="filter-group">
          <label>Timeframe</label>
          <select
            value={timeframe}
            onChange={e => setTimeframe(e.target.value)}
            style={{ background: 'var(--surface)', border: '1px solid var(--border)', color: 'var(--text)', borderRadius: 4, padding: '0.15rem 0.4rem', fontSize: '0.8rem' }}
          >
            {MAP_TIMEFRAMES.map(t => <option key={t.value} value={t.value}>{t.label}</option>)}
          </select>
        </div>
        <label style={{ display: 'flex', alignItems: 'center', gap: '0.35rem', fontSize: '0.8125rem', cursor: 'pointer' }}>
          <input type="checkbox" checked={localOnly} onChange={e => setLocalOnly(e.target.checked)} />
          Local nodes (0 hops)
        </label>
        <label style={{ display: 'flex', alignItems: 'center', gap: '0.35rem', fontSize: '0.8125rem', cursor: 'pointer' }}>
          <input type="checkbox" checked={showTrails} onChange={e => setShowTrails(e.target.checked)} />
          Show trails
        </label>
        {showTrails && (
          <>
            <select
              value={trailWindow}
              onChange={e => setTrailWindow(e.target.value)}
              style={{ background: 'var(--surface)', border: '1px solid var(--border)', color: 'var(--text)', borderRadius: 4, padding: '0.15rem 0.4rem', fontSize: '0.8rem' }}
            >
              {TRAIL_WINDOWS.map(w => <option key={w} value={w}>{w}</option>)}
            </select>
            <span style={{ fontSize: '0.8rem', color: 'var(--muted)' }}>
              {trailLoading ? 'Loading…' : `${totalPoints} point${totalPoints !== 1 ? 's' : ''}`}
            </span>
          </>
        )}
      </div>
      <div ref={mapRef} className="map-container" />
    </div>
  );
}

// ── API Docs ──────────────────────────────────────────────────────────────────

const API_ENDPOINTS = [
  {
    method: 'GET', path: '/api/packets',
    desc: 'All captured packets, newest first.',
    params: [
      { name: 'type',    desc: 'Filter by packet type (text, telemetry, nodeinfo, position, traceroute, neighborinfo)' },
      { name: 'node',    desc: 'Filter by sender node ID, e.g. !abc1234' },
      { name: 'channel', desc: 'Filter by channel index, e.g. 0' },
      { name: 'since',   desc: 'Time window: 1h, 6h, 24h, 7d' },
      { name: 'limit',   desc: 'Max rows returned (default 100, max 500)' },
    ],
    examples: [
      '/api/packets?type=text&since=1h',
      '/api/packets?node=!abc1234&limit=50',
      '/api/packets?channel=0&since=24h',
    ],
  },
  {
    method: 'GET', path: '/api/nodes',
    desc: 'All known nodes, ordered by last seen.',
    params: [],
    examples: ['/api/nodes'],
  },
  {
    method: 'GET', path: '/api/nodes/:id',
    desc: 'Single node detail plus its 50 most recent packets.',
    params: [
      { name: ':id', desc: 'Node ID, e.g. !abc1234' },
    ],
    examples: ['/api/nodes/!abc1234'],
  },
  {
    method: 'GET', path: '/api/messages',
    desc: 'Text messages only, newest first.',
    params: [
      { name: 'node',    desc: 'Filter by sender node ID' },
      { name: 'channel', desc: 'Filter by channel index, e.g. 0' },
      { name: 'since',   desc: 'Time window: 1h, 6h, 24h, 7d' },
      { name: 'limit',   desc: 'Max rows returned (default 100, max 500)' },
    ],
    examples: [
      '/api/messages?since=24h',
      '/api/messages?channel=0&limit=20',
    ],
  },
  {
    method: 'GET', path: '/api/stats',
    desc: 'Summary: packet counts by type, active nodes, last packet time.',
    params: [],
    examples: ['/api/stats'],
  },
  {
    method: 'GET', path: '/api/display/summary',
    desc: 'Minimal payload for e-ink / external displays. Returns last message, active node count, packet rate.',
    params: [],
    examples: ['/api/display/summary'],
  },
  {
    method: 'GET', path: '/api/health/nodes',
    desc: 'Online/offline status for every known node, based on last_seen vs. the offline threshold. Also returns online/offline/total counts.',
    params: [
      { name: 'hours', desc: 'Override the configured offline threshold for this request only, e.g. 1' },
    ],
    examples: ['/api/health/nodes', '/api/health/nodes?hours=1'],
  },
  {
    method: 'GET', path: '/api/health/nodes/:id',
    desc: "Single-node health check, meant for an external uptime monitor (e.g. Uptime Kuma's HTTP(s) monitor type — just paste the URL in, nothing else to configure). Responds 200 while the node's been heard from within the offline threshold, 503 once it's gone quiet longer than that, 404 if the node is unknown.",
    params: [
      { name: ':id',  desc: 'Node ID — decimal, bare hex, or !hex, e.g. !abc1234' },
      { name: 'hours', desc: 'Override the configured offline threshold for this request only' },
    ],
    examples: ['/api/health/nodes/!abc1234'],
  },
  {
    method: 'GET', path: '/api/health/config',
    desc: 'Current offline-threshold setting used by /api/health/nodes.',
    params: [],
    examples: ['/api/health/config'],
  },
  {
    method: 'PUT', path: '/api/health/config',
    desc: 'Set the offline threshold (hours a node can go unseen before it counts as offline). Persists across restarts.',
    params: [],
    examples: [],
    body: JSON.stringify({ offline_threshold_hours: 4 }, null, 2),
  },
  {
    method: 'GET',  path: '/api/rules',  desc: 'List all alert rules.',            params: [], examples: ['/api/rules'] },
  {
    method: 'POST', path: '/api/rules',
    desc: 'Create an alert rule.',
    params: [
      { name: 'name',        desc: 'Rule label' },
      { name: 'enabled',     desc: 'true / false' },
      { name: 'packet_type', desc: 'Packet type to match, or omit for any' },
      { name: 'from_node',   desc: 'Node ID to match, or omit for any' },
      { name: 'channel',     desc: 'Channel to match, or omit for any' },
      { name: 'keyword',     desc: 'Text keyword to match (text packets only), or omit for any' },
      { name: 'priority',    desc: 'Pushover priority -2 to 2' },
      { name: 'sound',       desc: 'Pushover sound name' },
    ],
    examples: [],
    body: JSON.stringify({ name: 'Emergency', enabled: true, packet_type: 'text', keyword: 'emergency', priority: 1, sound: 'siren' }, null, 2),
  },
  {
    method: 'PUT',    path: '/api/rules/:id', desc: 'Update an existing rule. Same body as POST.', params: [{ name: ':id', desc: 'Rule ID' }], examples: [] },
  {
    method: 'PATCH',  path: '/api/rules/:id/toggle', desc: 'Toggle a rule enabled/disabled.',       params: [{ name: ':id', desc: 'Rule ID' }], examples: [] },
  {
    method: 'DELETE', path: '/api/rules/:id', desc: 'Delete a rule.',                               params: [{ name: ':id', desc: 'Rule ID' }], examples: [] },
];

const METHOD_COLOR = {
  GET: 'var(--success)', POST: 'var(--accent)', PUT: 'var(--chart-yellow)',
  PATCH: 'var(--type-position)', DELETE: 'var(--danger)',
};

function TryIt({ path }) {
  const [result, setResult] = useState(null);
  const [loading, setLoading] = useState(false);

  const run = () => {
    setLoading(true);
    setResult(null);
    apiFetch(path)
      .then(d => setResult(JSON.stringify(d, null, 2)))
      .catch(e => setResult(`Error: ${e.message}`))
      .finally(() => setLoading(false));
  };

  return (
    <span style={{ display: 'inline-flex', alignItems: 'center', gap: '0.4rem' }}>
      <button className="btn btn-ghost btn-sm" onClick={run} disabled={loading} style={{ fontSize: '0.75rem' }}>
        {loading ? '…' : 'Try'}
      </button>
      {result && (
        <pre className="try-result">{result.length > 800 ? result.slice(0, 800) + '\n… (truncated)' : result}</pre>
      )}
    </span>
  );
}

function ApiDocs() {
  const base = window.location.origin;

  return (
    <div className="api-docs">
      <p className="api-intro">
        Base URL: <code className="mono">{base}</code>. All responses are JSON. Timestamps are UTC SQLite format (<code>YYYY-MM-DD HH:MM:SS</code>).
      </p>

      {API_ENDPOINTS.map((ep, i) => (
        <div key={i} className="api-endpoint">
          <div className="ep-header">
            <span className="ep-method" style={{ color: METHOD_COLOR[ep.method] }}>{ep.method}</span>
            <span className="ep-path mono">{ep.path}</span>
          </div>
          <p className="ep-desc">{ep.desc}</p>

          {ep.params.length > 0 && (
            <table className="ep-params">
              <thead><tr><th>Parameter</th><th>Description</th></tr></thead>
              <tbody>
                {ep.params.map(p => (
                  <tr key={p.name}>
                    <td><code className="mono">{p.name}</code></td>
                    <td className="muted">{p.desc}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}

          {ep.body && (
            <div className="ep-body-wrap">
              <span className="ep-label">Request body (JSON)</span>
              <pre className="ep-body">{ep.body}</pre>
            </div>
          )}

          {ep.examples.length > 0 && (
            <div className="ep-examples">
              <span className="ep-label">Examples</span>
              {ep.examples.map(ex => (
                <div key={ex} className="ep-example-row">
                  <code className="mono ep-example-path">{ex}</code>
                  <TryIt path={ex} />
                </div>
              ))}
            </div>
          )}
        </div>
      ))}
    </div>
  );
}

// ── Bot tab ───────────────────────────────────────────────────────────────────

const BOT_SUBVIEWS = ['Dashboard', 'Channels', 'Settings', 'Modules', 'Messages', 'LLM', 'Actions'];

function Bot() {
  const [sub, setSub] = usePersistentState('bot.subview', 'Dashboard');
  return (
    <div>
      <div className="bot-subnav" style={{ display: 'flex', gap: '0.5rem', marginBottom: '0.75rem', flexWrap: 'wrap' }}>
        {BOT_SUBVIEWS.map(s => (
          <button key={s}
                  onClick={() => setSub(s)}
                  className={sub === s ? 'active' : ''}
                  style={{
                    padding: '0.35rem 0.75rem',
                    background: sub === s ? 'var(--accent, #4a90e2)' : 'var(--bg-elev, #2a2a2a)',
                    color: sub === s ? 'var(--on-accent)' : 'inherit',
                    border: '1px solid var(--border, #444)',
                    borderRadius: 4,
                    cursor: 'pointer',
                  }}>
            {s}
          </button>
        ))}
      </div>
      {sub === 'Dashboard' && <BotDashboard />}
      {sub === 'Channels'  && <BotChannels />}
      {sub === 'Settings'  && <BotSettings />}
      {sub === 'Modules'   && <BotModules />}
      {sub === 'Messages'  && <BotMessages />}
      {sub === 'LLM'       && <BotLLM />}
      {sub === 'Actions'   && <BotActions />}
    </div>
  );
}

function BotDashboard() {
  const [status, setStatus] = useState(null);
  const [nodes, setNodes]   = useState([]);
  const [error, setError]   = useState(null);
  const [sendText, setSendText] = useState('');
  const [sendCh, setSendCh] = useState(0);
  const [sendBusy, setSendBusy] = useState(false);

  useEffect(() => {
    let alive = true;
    const load = () => {
      apiFetch('/api/bot/status')
        .then(d => { if (alive) { setStatus(d); setError(null); } })
        .catch(e => { if (alive) setError(e.message); });
      apiFetch('/api/bot/heard-nodes')
        .then(d => { if (alive) setNodes(Array.isArray(d) ? d : []); })
        .catch(() => {});
    };
    load();
    const id = setInterval(load, 5000);
    return () => { alive = false; clearInterval(id); };
  }, []);

  const send = async () => {
    if (!sendText.trim()) return;
    setSendBusy(true);
    try {
      const res = await fetch('/api/bot/send', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text: sendText, channel: Number(sendCh) }),
      });
      if (!res.ok) throw new Error(`${res.status}`);
      setSendText('');
    } catch (e) {
      setError(`Send failed: ${e.message}`);
    } finally { setSendBusy(false); }
  };

  const s = status || {};
  return (
    <div>
      {error && <div className="error-banner" style={{ marginBottom: '0.5rem' }}>{error}</div>}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(160px, 1fr))', gap: '0.75rem', marginBottom: '1rem' }}>
        <Card label="Status" value={status == null ? '…' : (s.enabled ? 'Enabled' : 'Disabled')} accent={s.enabled ? '#3a7' : '#a33'} />
        <Card label="Uptime" value={s.uptime ?? '—'} />
        <Card label="Bot node" value={s.bot_node || '(unset)'} />
        <Card label="Heard nodes" value={s.heard_nodes ?? '—'} />
        <Card label="Modules" value={s.module_count ?? '—'} />
        <Card label="LLM queue" value={(s.llm && s.llm.queue_depth) ?? 0} />
      </div>

      <h3>Send a message as the bot</h3>
      <div style={{ display: 'flex', gap: '0.5rem', marginBottom: '1rem', flexWrap: 'wrap' }}>
        <input type="text" placeholder="Message text"
               value={sendText} onChange={e => setSendText(e.target.value)}
               style={{ flex: 1, minWidth: 220, padding: '0.4rem' }}
               onKeyDown={e => e.key === 'Enter' && send()} />
        <input type="number" min="0" max="7" value={sendCh}
               onChange={e => setSendCh(e.target.value)}
               style={{ width: 70, padding: '0.4rem' }} />
        <button onClick={send} disabled={sendBusy || !sendText.trim()}>Send</button>
      </div>

      <h3>Heard nodes</h3>
      <table className="data-table">
        <thead><tr><th>ID</th><th>Name</th><th>Last seen</th><th>RSSI</th><th>Hops</th><th>Battery</th></tr></thead>
        <tbody>
          {nodes.map(n => (
            <tr key={n.id}>
              <td><code>{n.hex}</code></td>
              <td>{n.long_name || n.short_name || '—'}</td>
              <td>{fmtTs(n.last_seen)}</td>
              <td>{n.rssi ?? '—'}</td>
              <td>{n.hops_away ?? '—'}</td>
              <td>{n.battery_level != null ? `${n.battery_level}%` : '—'}</td>
            </tr>
          ))}
          {nodes.length === 0 && <tr><td colSpan="6" className="empty">{status == null ? 'Loading…' : 'No nodes heard yet'}</td></tr>}
        </tbody>
      </table>
    </div>
  );
}

function Card({ label, value, accent }) {
  return (
    <div style={{
      padding: '0.75rem 1rem',
      background: 'var(--bg-elev, #2a2a2a)',
      borderLeft: `3px solid ${accent || 'var(--accent, #4a90e2)'}`,
      borderRadius: 4,
    }}>
      <div style={{ fontSize: '0.75rem', color: 'var(--muted, #888)', textTransform: 'uppercase', letterSpacing: '0.05em' }}>{label}</div>
      <div style={{ fontSize: '1.25rem', fontWeight: 'bold', marginTop: '0.25rem' }}>{value}</div>
    </div>
  );
}

function BotSettings() {
  const [groups, setGroups] = useState({});
  const [values, setValues] = useState({});
  const [saving, setSaving] = useState(false);
  const [msg, setMsg] = useState(null);
  const aliveRef = useRef(true);

  const load = () => apiFetch('/api/bot/settings').then(d => {
    if (!aliveRef.current) return;
    setGroups(d.groups || {});
    const vals = {};
    (d.items || []).forEach(it => { vals[it.key] = it.value ?? ''; });
    setValues(vals);
  }).catch(e => { if (aliveRef.current) setMsg(`Error: ${e.message}`); });

  useEffect(() => {
    aliveRef.current = true;
    load();
    return () => { aliveRef.current = false; };
  }, []);

  const set = (k, v) => setValues(prev => ({ ...prev, [k]: v }));

  const save = async () => {
    setSaving(true); setMsg(null);
    try {
      const res = await fetch('/api/bot/settings', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(values),
      });
      if (!res.ok) throw new Error(`${res.status}`);
      setMsg('Saved.');
      load();
    } catch (e) { setMsg(`Save failed: ${e.message}`); }
    finally { setSaving(false); }
  };

  const reset = async (key) => {
    if (!confirm(`Reset ${key} to env/default?`)) return;
    await fetch(`/api/bot/settings/${key}`, { method: 'DELETE' });
    load();
  };

  return (
    <div>
      {msg && <div className="info-banner" style={{ marginBottom: '0.5rem' }}>{msg}</div>}
      {Object.entries(groups).map(([group, items]) => (
        <div key={group} style={{ marginBottom: '1.25rem' }}>
          <h3>{group}</h3>
          <table className="data-table">
            <tbody>
              {items.map(it => (
                <tr key={it.key}>
                  <td style={{ width: '40%' }}>
                    <div>{it.label}</div>
                    <div style={{ fontSize: '0.7rem', color: 'var(--muted, #888)' }}>{it.key}</div>
                  </td>
                  <td>
                    {it.kind === 'bool' ? (
                      <input type="checkbox"
                             checked={String(values[it.key]).toLowerCase() === 'true'}
                             onChange={e => set(it.key, e.target.checked ? 'true' : 'false')} />
                    ) : it.kind === 'textarea' ? (
                      <textarea rows="3" style={{ width: '100%' }}
                                value={values[it.key] ?? ''}
                                onChange={e => set(it.key, e.target.value)} />
                    ) : (
                      <input type={it.kind === 'int' ? 'number' : 'text'}
                             style={{ width: '100%' }}
                             value={values[it.key] ?? ''}
                             onChange={e => set(it.key, e.target.value)} />
                    )}
                  </td>
                  <td style={{ width: 80 }}>
                    {it.overridden && (
                      <button onClick={() => reset(it.key)} title="Reset to env/default">↺</button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ))}
      <button onClick={save} disabled={saving} style={{ padding: '0.5rem 1.5rem' }}>
        {saving ? 'Saving…' : 'Save all'}
      </button>
    </div>
  );
}

function BotChannels() {
  const [policies, setPolicies] = useState([]);
  const [editing, setEditing] = useState(null);   // policy being edited
  const [error, setError] = useState(null);
  const aliveRef = useRef(true);

  const load = () => apiFetch('/api/bot/channels')
    .then(d => { if (aliveRef.current) setPolicies(Array.isArray(d) ? d : []); })
    .catch(e => { if (aliveRef.current) setError(e.message); });

  useEffect(() => {
    aliveRef.current = true;
    load();
    return () => { aliveRef.current = false; };
  }, []);

  const blank = { channel: '', name: '', listen: true, respond: true, is_response_ch: false, allowed_cmds: [] };

  const save = async (pol) => {
    try {
      const res = await fetch('/api/bot/channels', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ...pol, channel: Number(pol.channel) }),
      });
      if (!res.ok) throw new Error((await res.json()).error || res.statusText);
      setEditing(null); load();
    } catch (e) { setError(e.message); }
  };

  const del = async (ch) => {
    if (!confirm(`Delete policy for channel ${ch}?`)) return;
    await fetch(`/api/bot/channels/${ch}`, { method: 'DELETE' });
    load();
  };

  return (
    <div>
      {error && <div className="error-banner">{error}</div>}
      <p style={{ color: 'var(--muted, #888)' }}>
        Control which channels the bot listens on and responds to. A channel without a policy
        defaults to listen=on, respond=on, response-channel=incoming.
        Allowed commands is a comma-separated list (e.g. <code>ping,help,weather</code>); leave empty for "all".
      </p>

      <table className="data-table">
        <thead>
          <tr>
            <th>Ch#</th><th>Name</th><th>Listen</th><th>Respond</th>
            <th>Default reply</th><th>Allowed cmds</th><th></th>
          </tr>
        </thead>
        <tbody>
          {policies.map(p => (
            <tr key={p.channel}>
              <td>{p.channel}</td>
              <td>{p.name || '—'}</td>
              <td>{p.listen ? '✓' : '✗'}</td>
              <td>{p.respond ? '✓' : '✗'}</td>
              <td>{p.is_response_ch ? '★' : ''}</td>
              <td><code>{(p.allowed_cmds || []).join(',') || '(all)'}</code></td>
              <td>
                <button onClick={() => setEditing(p)}>Edit</button>
                <button onClick={() => del(p.channel)} style={{ marginLeft: 4 }}>Delete</button>
              </td>
            </tr>
          ))}
          {policies.length === 0 && <tr><td colSpan="7" className="empty">No channel policies — all channels behave as default.</td></tr>}
        </tbody>
      </table>

      <button onClick={() => setEditing({ ...blank })} style={{ marginTop: '0.75rem' }}>+ Add policy</button>

      {editing && <ChannelEditor pol={editing} onSave={save} onCancel={() => setEditing(null)} />}
    </div>
  );
}

function ChannelEditor({ pol, onSave, onCancel }) {
  const [p, setP] = useState(pol);
  const upd = (k, v) => setP(prev => ({ ...prev, [k]: v }));
  return (
    <div style={{ marginTop: '1rem', padding: '1rem', background: 'var(--bg-elev, #2a2a2a)', borderRadius: 4 }}>
      <h4>Channel policy</h4>
      <div style={{ display: 'grid', gridTemplateColumns: '120px 1fr', gap: '0.5rem', alignItems: 'center' }}>
        <label>Channel index</label>
        <input type="number" value={p.channel} onChange={e => upd('channel', e.target.value)} />
        <label>Name (label only)</label>
        <input type="text" value={p.name || ''} onChange={e => upd('name', e.target.value)} />
        <label>Listen on this channel</label>
        <input type="checkbox" checked={!!p.listen} onChange={e => upd('listen', e.target.checked)} />
        <label>Respond on this channel</label>
        <input type="checkbox" checked={!!p.respond} onChange={e => upd('respond', e.target.checked)} />
        <label>Use as default reply ch</label>
        <input type="checkbox" checked={!!p.is_response_ch} onChange={e => upd('is_response_ch', e.target.checked)} />
        <label>Allowed cmds (comma-sep, blank = all)</label>
        <input type="text"
               value={(p.allowed_cmds || []).join(',')}
               onChange={e => upd('allowed_cmds', e.target.value.split(',').map(s => s.trim()).filter(Boolean))} />
      </div>
      <div style={{ marginTop: '0.75rem' }}>
        <button onClick={() => onSave(p)}>Save</button>
        <button onClick={onCancel} style={{ marginLeft: 4 }}>Cancel</button>
      </div>
    </div>
  );
}

function BotModules() {
  const [mods, setMods]   = useState([]);
  const [editing, setEditing] = useState(null);
  const [error, setError] = useState(null);
  const aliveRef = useRef(true);

  const load = () => apiFetch('/api/bot/modules')
    .then(d => { if (aliveRef.current) setMods(Array.isArray(d) ? d : []); })
    .catch(e => { if (aliveRef.current) setError(e.message); });

  useEffect(() => {
    aliveRef.current = true;
    load();
    return () => { aliveRef.current = false; };
  }, []);

  const blank = { name: '', kind: 'text', description: '', aliases: '',
                  enabled: true, config: { template: '' } };

  const save = async (m) => {
    const body = {
      ...m,
      aliases: typeof m.aliases === 'string'
        ? m.aliases.split(',').map(s => s.trim()).filter(Boolean)
        : m.aliases,
    };
    const url  = m.id ? `/api/bot/modules/${m.id}` : '/api/bot/modules';
    const method = m.id ? 'PUT' : 'POST';
    try {
      const res = await fetch(url, {
        method,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      if (!res.ok) throw new Error((await res.json()).error || res.statusText);
      setEditing(null); load();
    } catch (e) { setError(e.message); }
  };

  const del = async (id) => {
    if (!confirm('Delete module?')) return;
    await fetch(`/api/bot/modules/${id}`, { method: 'DELETE' });
    load();
  };
  const toggle = async (id) => {
    await fetch(`/api/bot/modules/${id}/toggle`, { method: 'POST' });
    load();
  };

  return (
    <div>
      {error && <div className="error-banner">{error}</div>}
      <table className="data-table">
        <thead><tr><th>Name</th><th>Kind</th><th>Enabled</th><th>Description</th><th></th></tr></thead>
        <tbody>
          {mods.map(m => (
            <tr key={m.id}>
              <td><code>{m.name}</code> {m.aliases.length > 0 && <small>({m.aliases.join(',')})</small>}</td>
              <td>{m.kind}</td>
              <td><input type="checkbox" checked={m.enabled} onChange={() => toggle(m.id)} /></td>
              <td>{m.description}</td>
              <td>
                <button onClick={() => setEditing(m)}>Edit</button>
                <button onClick={() => del(m.id)} style={{ marginLeft: 4 }}>Del</button>
              </td>
            </tr>
          ))}
          {mods.length === 0 && <tr><td colSpan="5" className="empty">No custom modules</td></tr>}
        </tbody>
      </table>
      <button onClick={() => setEditing({ ...blank })} style={{ marginTop: '0.75rem' }}>+ New module</button>

      {editing && <ModuleEditor mod={editing} onSave={save} onCancel={() => setEditing(null)} />}
    </div>
  );
}

function ModuleEditor({ mod, onSave, onCancel }) {
  const [m, setM] = useState({
    ...mod,
    aliases: Array.isArray(mod.aliases) ? mod.aliases.join(',') : (mod.aliases || ''),
    config: mod.config || {},
  });
  const upd  = (k, v)  => setM(p => ({ ...p, [k]: v }));
  const updC = (k, v)  => setM(p => ({ ...p, config: { ...p.config, [k]: v } }));

  const k = m.kind;
  return (
    <div style={{ marginTop: '1rem', padding: '1rem', background: 'var(--bg-elev, #2a2a2a)', borderRadius: 4 }}>
      <h4>{m.id ? `Edit ${m.name}` : 'New module'}</h4>
      <div style={{ display: 'grid', gridTemplateColumns: '140px 1fr', gap: '0.5rem', alignItems: 'center' }}>
        <label>Name</label>
        <input value={m.name} onChange={e => upd('name', e.target.value)} />
        <label>Kind</label>
        <select value={m.kind} onChange={e => upd('kind', e.target.value)}>
          <option value="text">text</option>
          <option value="http">http</option>
          <option value="shell">shell</option>
          <option value="network">network</option>
          <option value="python">python</option>
        </select>
        <label>Enabled</label>
        <input type="checkbox" checked={!!m.enabled} onChange={e => upd('enabled', e.target.checked)} />
        <label>Aliases (comma)</label>
        <input value={m.aliases} onChange={e => upd('aliases', e.target.value)} />
        <label>Description</label>
        <input value={m.description || ''} onChange={e => upd('description', e.target.value)} />

        {k === 'text' && <>
          <label>Template</label>
          <textarea rows="3" value={m.config.template || ''} onChange={e => updC('template', e.target.value)} />
        </>}
        {k === 'http' && <>
          <label>URL</label>
          <input value={m.config.url || ''} onChange={e => updC('url', e.target.value)} />
          <label>Method</label>
          <select value={m.config.method || 'GET'} onChange={e => updC('method', e.target.value)}>
            <option>GET</option><option>POST</option>
          </select>
          <label>JSON path (optional)</label>
          <input value={m.config.json_path || ''} onChange={e => updC('json_path', e.target.value)} />
          <label>Regex (optional)</label>
          <input value={m.config.regex || ''} onChange={e => updC('regex', e.target.value)} />
          <label>Output template</label>
          <input value={m.config.output_template || ''} onChange={e => updC('output_template', e.target.value)} />
        </>}
        {k === 'shell' && <>
          <label>Command</label>
          <input value={m.config.command || ''} onChange={e => updC('command', e.target.value)} />
          <label>Pass user args</label>
          <input type="checkbox" checked={!!m.config.pass_args} onChange={e => updC('pass_args', e.target.checked)} />
        </>}
        {k === 'network' && <>
          <label>Op</label>
          <select value={m.config.op || 'ping'} onChange={e => updC('op', e.target.value)}>
            <option value="ping">ping</option>
            <option value="port">port</option>
          </select>
          <label>Host</label>
          <input value={m.config.host || ''} onChange={e => updC('host', e.target.value)} />
          {m.config.op === 'port' && <>
            <label>Port</label>
            <input type="number" value={m.config.port || ''} onChange={e => updC('port', e.target.value)} />
          </>}
        </>}
        {k === 'python' && <>
          <label>Code (must set <code>result</code>)</label>
          <textarea rows="8" value={m.config.code || ''}
                    onChange={e => updC('code', e.target.value)}
                    style={{ fontFamily: 'monospace' }} />
        </>}
      </div>
      <div style={{ marginTop: '0.75rem' }}>
        <button onClick={() => onSave(m)}>Save</button>
        <button onClick={onCancel} style={{ marginLeft: 4 }}>Cancel</button>
      </div>
    </div>
  );
}

function BotMessages() {
  const [msgs, setMsgs] = useState([]);
  const [q, setQ] = useState('');
  const [tick, setTick] = useState(0);
  const [error, setError] = useState(null);

  useEffect(() => {
    let alive = true;
    const load = () => {
      const p = new URLSearchParams();
      if (q) p.set('q', q);
      p.set('limit', '300');
      apiFetch(`/api/bot/messages?${p}`)
        .then(d => { if (alive) { setMsgs(Array.isArray(d) ? d : []); setError(null); } })
        .catch(e => { if (alive) setError(e.message); });
    };
    load();
    const id = setInterval(load, 5000);
    return () => { alive = false; clearInterval(id); };
  }, [q, tick]);

  // setQ(q => q) doesn't re-run the effect (same value); bump a counter instead.
  const reload = () => setTick(t => t + 1);

  return (
    <div>
      {error && <div className="error-banner">{error}</div>}
      <div style={{ marginBottom: '0.5rem' }}>
        <input placeholder="Search…" value={q} onChange={e => setQ(e.target.value)}
               style={{ width: 240, padding: '0.4rem' }} />
        <button onClick={reload} style={{ marginLeft: 4 }}>Search</button>
      </div>
      <table className="data-table">
        <thead><tr><th>When</th><th>Dir</th><th>Node</th><th>Ch</th><th>Text</th></tr></thead>
        <tbody>
          {msgs.map(m => (
            <tr key={m.id}>
              <td title={fmtFull(m.ts)}>{fmtTs(m.ts)}</td>
              <td>{m.direction === 'in' ? '⇦' : '⇨'}</td>
              <td><code>{m.node_id ? (typeof m.node_id === 'number' ? `!${m.node_id.toString(16).padStart(8,'0')}` : String(m.node_id)) : '—'}</code>{m.node_name && <small> ({m.node_name})</small>}</td>
              <td>{m.channel ?? '—'}</td>
              <td style={{ whiteSpace: 'pre-wrap' }}>{m.text}</td>
            </tr>
          ))}
          {msgs.length === 0 && <tr><td colSpan="5" className="empty">No messages logged yet</td></tr>}
        </tbody>
      </table>
    </div>
  );
}

function BotLLM() {
  const [health, setHealth] = useState(null);
  const [sessions, setSessions] = useState([]);
  const [prompt, setPrompt] = useState('Say hello in 5 words.');
  const [answer, setAnswer] = useState(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let alive = true;
    const load = () => {
      apiFetch('/api/bot/llm/health').then(d => { if (alive) setHealth(d); }).catch(() => {});
      apiFetch('/api/bot/llm/sessions').then(d => { if (alive) setSessions(Array.isArray(d) ? d : []); }).catch(() => {});
    };
    load();
    const id = setInterval(load, 5000);
    return () => { alive = false; clearInterval(id); };
  }, []);

  const run = async () => {
    setBusy(true); setAnswer(null);
    try {
      const res = await fetch('/api/bot/llm/test', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ prompt }),
      });
      const data = await res.json();
      setAnswer(data.ok ? data.answer : `Error: ${data.error}`);
    } catch (e) { setAnswer(`Error: ${e.message}`); }
    finally { setBusy(false); }
  };

  return (
    <div>
      <h3>Health</h3>
      {health ? (
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(140px, 1fr))', gap: '0.5rem', marginBottom: '1rem' }}>
          <Card label="Queue depth"  value={health.queue_depth} />
          <Card label="Last latency" value={health.last_latency_ms != null ? `${health.last_latency_ms}ms` : '—'} />
          <Card label="OK / Failed"  value={`${health.total_ok} / ${health.total_failed}`} />
          <Card label="Last ok"      value={fmtTs(health.last_ok)} />
          <Card label="Last error"   value={health.last_error || '—'} />
        </div>
      ) : <div className="empty">Loading…</div>}

      <h3>Active sessions</h3>
      <table className="data-table">
        <thead><tr><th>Node</th><th>Turns</th><th>Last active</th></tr></thead>
        <tbody>
          {sessions.map(s => (
            <tr key={s.node_id}>
              <td>{s.node_id ? `!${Number(s.node_id).toString(16).padStart(8,'0')}` : 'anon'}</td>
              <td>{s.turns}</td>
              <td>{fmtTs(s.last_active)}</td>
            </tr>
          ))}
          {sessions.length === 0 && <tr><td colSpan="3" className="empty">No active sessions</td></tr>}
        </tbody>
      </table>

      <h3 style={{ marginTop: '1rem' }}>Test prompt</h3>
      <textarea rows="2" style={{ width: '100%', padding: '0.4rem' }}
                value={prompt} onChange={e => setPrompt(e.target.value)} />
      <button onClick={run} disabled={busy} style={{ marginTop: '0.5rem' }}>
        {busy ? 'Querying…' : 'Ask LLM'}
      </button>
      {answer && (
        <pre style={{ background: 'var(--bg-elev, #2a2a2a)', padding: '0.75rem',
                      borderRadius: 4, marginTop: '0.5rem', whiteSpace: 'pre-wrap' }}>
          {answer}
        </pre>
      )}
    </div>
  );
}

function BotActionCard({ title, description, buttonLabel, path, renderResult }) {
  const [busy, setBusy]     = useState(false);
  const [result, setResult] = useState(null);

  const run = async () => {
    setBusy(true); setResult(null);
    try {
      const res = await fetch(path, { method: 'POST' });
      const data = await res.json();
      setResult(data);
    } catch (e) {
      setResult({ ok: false, error: e.message });
    } finally { setBusy(false); }
  };

  return (
    <div style={{ background: 'var(--bg-elev, #2a2a2a)', border: '1px solid var(--border, #444)',
                  borderRadius: 4, padding: '0.75rem 1rem', marginBottom: '0.75rem' }}>
      <h3 style={{ marginTop: 0 }}>{title}</h3>
      <p style={{ color: 'var(--muted, #888)', marginTop: '-0.25rem' }}>{description}</p>
      <button onClick={run} disabled={busy}>{busy ? 'Running…' : buttonLabel}</button>
      {result && (
        <div style={{ marginTop: '0.5rem' }}>
          {result.ok === false && (
            <div className="error-banner">{result.error || 'Failed'}</div>
          )}
          {result.ok !== false && renderResult && (
            <pre style={{ background: 'var(--bg, #1a1a1a)', padding: '0.5rem 0.75rem',
                          borderRadius: 4, whiteSpace: 'pre-wrap', margin: 0 }}>
              {renderResult(result)}
            </pre>
          )}
        </div>
      )}
    </div>
  );
}

function BotActions() {
  return (
    <div>
      <p style={{ color: 'var(--muted, #888)' }}>
        Manually fire background bot features right now, instead of waiting on their
        schedule — useful for testing that a feature actually works.
      </p>

      <BotActionCard
        title="Daily weather alert"
        description="Runs the DEFAULT_LOCATION weather check now and broadcasts on the response channel if alert-worthy, ignoring WEATHER_ALERT_HOUR."
        buttonLabel="Run weather check now"
        path="/api/bot/actions/weather-check"
        renderResult={r => r.sent
          ? `Sent: ${r.alert}`
          : 'No alert-worthy weather right now — nothing sent.'}
      />

      <BotActionCard
        title="Per-node weather DM"
        description="Runs the per-node weather DM check now for every watched node, ignoring the once-a-day guard for nodes already alerted today."
        buttonLabel="Run per-node check now"
        path="/api/bot/actions/weather-check-per-node"
        renderResult={r => `Checked ${r.checked} node(s).\n` + (
          r.messaged && r.messaged.length
            ? `Sent DMs to: ${r.messaged.join(', ')}`
            : 'No alert-worthy weather for any checked node — nothing sent.'
        )}
      />
    </div>
  );
}

// ── Diagnostics ───────────────────────────────────────────────────────────────

function Diagnostics() {
  const [status,     setStatus]     = useState(null);
  const [log,        setLog]        = useState([]);
  const [error,      setError]      = useState(null);
  const [paused,     setPaused]     = useState(false);
  const [autoScroll, setAutoScroll] = useState(true);
  const [testText,   setTestText]   = useState('');
  const [testState,  setTestState]  = useState(null); // null | 'sending' | 'ok' | string(err)
  const logEndRef  = useRef(null);
  const pausedRef  = useRef(false);

  useEffect(() => { pausedRef.current = paused; }, [paused]);

  useEffect(() => {
    let alive = true;
    const loadStatus = () =>
      apiFetch('/api/status')
        .then(d  => { if (alive) { setStatus(d); setError(null); } })
        .catch(e => { if (alive) setError(e.message); });
    const loadLog = () => {
      if (pausedRef.current) return;
      apiFetch('/api/serial/log?limit=300')
        .then(d => { if (alive) setLog(d); })
        .catch(() => {});
    };
    loadStatus();
    loadLog();
    const si = setInterval(loadStatus, 5000);
    const li = setInterval(loadLog, 2000);
    return () => { alive = false; clearInterval(si); clearInterval(li); };
  }, []);

  useEffect(() => {
    if (autoScroll && logEndRef.current) {
      logEndRef.current.scrollIntoView({ behavior: 'instant' });
    }
  }, [log, autoScroll]);

  const sendTest = async () => {
    const text = testText.trim() || 'ping';
    setTestState('sending');
    try {
      const res  = await fetch('/api/send', {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({ text, channel: 0 }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || res.statusText);
      setTestState('ok');
      setTimeout(() => setTestState(null), 4000);
    } catch (e) {
      setTestState(e.message);
      setTimeout(() => setTestState(null), 5000);
    }
  };

  const s    = status || {};
  const node = s.local_node || {};

  // Fixed colors (not theme variables): this console keeps a dark
  // "terminal" background in both themes, so its text must stay legible
  // against that fixed dark background rather than flipping with the page.
  const LEVEL_COLOR = {
    rx:    '#56d364',
    tx:    '#58a6ff',
    info:  '#c9d1d9',
    warn:  '#ffa657',
    error: '#f85149',
  };

  return (
    <div>
      {error && <div className="error-banner">Error: {error}</div>}

      <div style={{ fontSize: '0.75rem', fontWeight: 600, color: 'var(--muted)', textTransform: 'uppercase', letterSpacing: '0.05em', marginBottom: '0.5rem' }}>
        Serial Status
      </div>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(150px, 1fr))', gap: '0.75rem', marginBottom: '1.5rem' }}>
        <Card label="Serial Port"  value={s.serial_port || '—'} />
        <Card
          label="Connection"
          value={status == null ? '…' : s.serial_connected ? 'Connected' : 'Disconnected'}
          accent={s.serial_connected ? '#56d364' : '#f85149'}
        />
        <Card label="Local Node"  value={node.node_id  || (node.error ? 'Error' : '—')} />
        <Card label="Node Name"   value={node.long_name || node.short_name || '—'} />
        {node.hw_model && <Card label="Hardware" value={node.hw_model} />}
      </div>

      {(() => {
        const bn = s.bot_node || {};
        const ok = bn.matches_local;
        return (
          <React.Fragment>
            <div style={{ fontSize: '0.75rem', fontWeight: 600, color: 'var(--muted)', textTransform: 'uppercase', letterSpacing: '0.05em', marginBottom: '0.5rem' }}>
              Bot Identity
            </div>
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(150px, 1fr))', gap: '0.75rem', marginBottom: ok ? '1.5rem' : '0.5rem' }}>
              <Card
                label="Bot Node (effective)"
                value={bn.effective_hex || (bn.effective_number ? bn.effective_number : 'unset')}
                accent={ok ? '#56d364' : '#f85149'}
              />
              <Card
                label="Source"
                value={bn.configured ? 'BOT_NODE_NUMBER setting' : (bn.effective_number ? 'auto-detected' : 'none')}
              />
              <Card
                label="Matches Local Radio"
                value={status == null ? '…' : (ok ? 'Yes' : 'No')}
                accent={ok ? '#56d364' : '#f85149'}
              />
            </div>
            {/* Only flag a mismatch once the radio is connected and identified —
                during startup/disconnect the comparison is meaningless. */}
            {status != null && s.serial_connected && node.node_id && !ok && (
              <div className="error-banner" style={{ marginBottom: '1.5rem' }}>
                Bot node number ({bn.effective_hex || bn.effective_number || 'unset'}) does not match the
                connected radio ({node.node_id || '—'}). Direct messages to the bot will be treated as
                channel messages (replies go to the channel, command prefix still required). Fix:
                set <strong>Bot node number</strong> in the Bot → Settings tab to <code>{node.node_id ? parseInt(node.node_id.replace('!',''), 16) : '?'}</code>.
              </div>
            )}
          </React.Fragment>
        );
      })()}

      <div style={{ fontSize: '0.75rem', fontWeight: 600, color: 'var(--muted)', textTransform: 'uppercase', letterSpacing: '0.05em', marginBottom: '0.5rem' }}>
        Test Transmission
      </div>
      <div style={{ display: 'flex', gap: '0.5rem', alignItems: 'center', marginBottom: '1.5rem', flexWrap: 'wrap' }}>
        <input
          type="text"
          placeholder="Message (default: ping)"
          value={testText}
          onChange={e => setTestText(e.target.value)}
          onKeyDown={e => e.key === 'Enter' && sendTest()}
          style={{ width: 240, padding: '0.35rem 0.5rem', background: 'var(--surface)', border: '1px solid var(--border)', color: 'var(--text)', borderRadius: 4, fontSize: '0.875rem' }}
        />
        <button
          className="btn btn-primary btn-sm"
          onClick={sendTest}
          disabled={testState === 'sending'}
        >
          {testState === 'sending' ? '…' : 'Send to ch 0'}
        </button>
        {testState === 'ok' && (
          <span style={{ color: '#56d364', fontSize: '0.85rem' }}>Sent — watch Serial Monitor for TX/RX activity</span>
        )}
        {testState && testState !== 'ok' && testState !== 'sending' && (
          <span style={{ color: '#f85149', fontSize: '0.85rem' }}>{testState}</span>
        )}
      </div>

      <div style={{ display: 'flex', alignItems: 'center', gap: '0.75rem', marginBottom: '0.5rem', flexWrap: 'wrap' }}>
        <div style={{ fontSize: '0.75rem', fontWeight: 600, color: 'var(--muted)', textTransform: 'uppercase', letterSpacing: '0.05em' }}>
          Serial Monitor
        </div>
        <span className="auto-badge">auto-refresh 2s</span>
        <span style={{ fontSize: '0.8rem', color: 'var(--muted)' }}>{log.length} entries</span>
        <label style={{ display: 'flex', alignItems: 'center', gap: '0.3rem', fontSize: '0.8125rem', cursor: 'pointer' }}>
          <input type="checkbox" checked={paused} onChange={e => setPaused(e.target.checked)} />
          Pause
        </label>
        <label style={{ display: 'flex', alignItems: 'center', gap: '0.3rem', fontSize: '0.8125rem', cursor: 'pointer' }}>
          <input type="checkbox" checked={autoScroll} onChange={e => setAutoScroll(e.target.checked)} />
          Auto-scroll
        </label>
        <button className="btn btn-ghost btn-sm" onClick={() => setLog([])}>Clear</button>
      </div>

      <div style={{
        background:   '#0d1117',
        border:       '1px solid var(--border)',
        borderRadius: 4,
        padding:      '0.5rem 0.75rem',
        fontFamily:   'monospace',
        fontSize:     '0.78rem',
        height:       400,
        overflowY:    'auto',
      }}>
        {log.length === 0
          ? <span style={{ color: '#8b949e' }}>No serial events yet — waiting for connection…</span>
          : log.map((entry, i) => (
            <div key={i} style={{ display: 'flex', gap: '0.75rem', lineHeight: '1.6' }}>
              <span style={{ color: '#8b949e', whiteSpace: 'nowrap', flexShrink: 0 }}>{entry.ts}</span>
              <span style={{ color: LEVEL_COLOR[entry.level] || '#c9d1d9', whiteSpace: 'nowrap', flexShrink: 0, minWidth: 52 }}>
                [{(entry.level || '').toUpperCase()}]
              </span>
              <span style={{ color: '#c9d1d9', wordBreak: 'break-word' }}>{entry.msg}</span>
            </div>
          ))
        }
        <div ref={logEndRef} />
      </div>
    </div>
  );
}

// ── Local node (the radio plugged into this host) ────────────────────────────

function DeviceChannels({ connected }) {
  const [channels, setChannels] = useState([]);
  const [error, setError]       = useState(null);
  const [loading, setLoading]   = useState(true);
  const [editIndex, setEditIndex] = useState(null);
  const [editName, setEditName]   = useState('');
  const [newName, setNewName]     = useState('');
  const [useCustomPsk, setUseCustomPsk] = useState(false);
  const [newPsk, setNewPsk]       = useState('');
  const [pskEditIndex, setPskEditIndex] = useState(null);
  const [pskValue, setPskValue]   = useState('');
  const [busy, setBusy]           = useState(false);

  const load = useCallback(() => {
    apiFetch('/api/device/channels')
      .then(d => { setChannels(Array.isArray(d) ? d : []); setError(null); })
      .catch(e => setError(e.message))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => { load(); }, [load]);

  const startEdit  = (ch) => { setEditIndex(ch.index); setEditName(ch.name); };
  const cancelEdit = () => { setEditIndex(null); setEditName(''); };

  const startPskEdit  = (ch) => { setPskEditIndex(ch.index); setPskValue(''); };
  const cancelPskEdit = () => { setPskEditIndex(null); setPskValue(''); };

  const patch = async (index, body) => {
    const res = await fetch(`/api/device/channels/${index}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const d = await res.json();
    if (!res.ok) throw new Error(d.error || res.statusText);
    return d;
  };

  const saveName = async (ch) => {
    const name = editName.trim();
    if (!name) return;
    if (ch.role === 'PRIMARY' && !confirm(
      "Rename the PRIMARY channel? Every other node on this mesh must be changed to match, " +
      "or it will stop being able to talk to this one."
    )) return;
    setBusy(true);
    try {
      await patch(ch.index, { name });
      cancelEdit();
      await load();
    } catch (e) { setError(e.message); }
    finally { setBusy(false); }
  };

  const toggleFlag = async (ch, key) => {
    setBusy(true);
    try {
      await patch(ch.index, { [key]: !ch[key] });
      await load();
    } catch (e) { setError(e.message); }
    finally { setBusy(false); }
  };

  const addChannel = async () => {
    const name = newName.trim();
    if (!name) return;
    if (useCustomPsk && !newPsk.trim()) {
      setError('Enter a key, or uncheck "Set my own key" to auto-generate one');
      return;
    }
    setBusy(true);
    try {
      const body = { name };
      if (useCustomPsk) body.psk = newPsk.trim();
      const res = await fetch('/api/device/channels', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      const d = await res.json();
      if (!res.ok) throw new Error(d.error || res.statusText);
      setNewName(''); setNewPsk(''); setUseCustomPsk(false);
      await load();
    } catch (e) { setError(e.message); }
    finally { setBusy(false); }
  };

  const replacePsk = async (ch) => {
    if (!confirm(
      ch.role === 'PRIMARY'
        ? "Replace the PRIMARY channel's encryption key? Every other node on this mesh must be updated to match, or it will stop being able to talk to this one."
        : `Replace the encryption key for "${ch.name}"? Any node currently using it will need the new key to stay connected.`
    )) return;
    setBusy(true);
    try {
      const res = await fetch(`/api/device/channels/${ch.index}/psk`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(pskValue.trim() ? { psk: pskValue.trim() } : {}),
      });
      const d = await res.json();
      if (!res.ok) throw new Error(d.error || res.statusText);
      cancelPskEdit();
      await load();
    } catch (e) { setError(e.message); }
    finally { setBusy(false); }
  };

  const removeChannel = async (ch) => {
    if (!confirm(`Delete channel "${ch.name}"? Any node using it will lose access to it.`)) return;
    setBusy(true);
    try {
      const res = await fetch(`/api/device/channels/${ch.index}`, { method: 'DELETE' });
      const d = await res.json();
      if (!res.ok) throw new Error(d.error || res.statusText);
      await load();
    } catch (e) { setError(e.message); }
    finally { setBusy(false); }
  };

  if (loading) return <div className="empty">Loading channels…</div>;

  return (
    <div style={{ marginBottom: '1.5rem' }}>
      {error && <div className="error-banner">{error}</div>}
      {!connected && channels.length === 0 &&
        <div className="empty">Radio not connected — no channel data available.</div>}
      {channels.length > 0 && (
        <div className="table-wrap">
          <table className="data-table">
            <thead>
              <tr><th>#</th><th>Role</th><th>Name</th><th>Encrypted</th><th>Uplink</th><th>Downlink</th><th></th></tr>
            </thead>
            <tbody>
              {channels.map(ch => (
                <React.Fragment key={ch.index}>
                  <tr>
                    <td className="mono">{ch.index}</td>
                    <td className="mono">{ch.role}</td>
                    <td>
                      {editIndex === ch.index ? (
                        <span style={{ display: 'inline-flex', gap: '0.35rem', alignItems: 'center' }}>
                          <input value={editName} onChange={e => setEditName(e.target.value)}
                                 maxLength={10} style={{ width: 100, padding: '0.2rem' }} />
                          <button onClick={() => saveName(ch)} disabled={busy || !editName.trim()}>Save</button>
                          <button onClick={cancelEdit} disabled={busy}>Cancel</button>
                        </span>
                      ) : (
                        <span>
                          {ch.name || <span className="muted">(default)</span>}
                          {ch.role !== 'DISABLED' && (
                            <button onClick={() => startEdit(ch)} disabled={busy} style={{ marginLeft: '0.5rem' }}>Rename</button>
                          )}
                        </span>
                      )}
                    </td>
                    <td className="mono" title={ch.has_psk ? 'Encryption key set' : 'No encryption key'}>
                      {ch.has_psk ? '🔒' : '—'}
                      {ch.role !== 'DISABLED' && (
                        <button onClick={() => (pskEditIndex === ch.index ? cancelPskEdit() : startPskEdit(ch))}
                                disabled={busy} style={{ marginLeft: '0.5rem' }}>
                          {pskEditIndex === ch.index ? 'Cancel' : 'Replace key'}
                        </button>
                      )}
                    </td>
                    <td>
                      {ch.role !== 'DISABLED' &&
                        <input type="checkbox" checked={!!ch.uplink_enabled} disabled={busy}
                               onChange={() => toggleFlag(ch, 'uplink_enabled')} />}
                    </td>
                    <td>
                      {ch.role !== 'DISABLED' &&
                        <input type="checkbox" checked={!!ch.downlink_enabled} disabled={busy}
                               onChange={() => toggleFlag(ch, 'downlink_enabled')} />}
                    </td>
                    <td>
                      {ch.role === 'SECONDARY' && (
                        <button onClick={() => removeChannel(ch)} disabled={busy} style={{ color: 'var(--danger)' }}>Delete</button>
                      )}
                    </td>
                  </tr>
                  {pskEditIndex === ch.index && (
                    <tr>
                      <td colSpan={7} style={{ background: 'var(--bg, #1a1a1a)', padding: '0.75rem 1rem' }}>
                        <div style={{ display: 'flex', gap: '0.5rem', alignItems: 'center', flexWrap: 'wrap' }}>
                          <input type="text" value={pskValue} onChange={e => setPskValue(e.target.value)}
                                 placeholder="Paste key (base64 or 0x-hex) — leave blank to auto-generate"
                                 style={{ padding: '0.4rem', width: 380 }} />
                          <button onClick={() => replacePsk(ch)} disabled={busy}>
                            {pskValue.trim() ? 'Set key' : 'Generate random key'}
                          </button>
                        </div>
                        <div style={{ fontSize: '0.8rem', color: 'var(--muted)', marginTop: '0.35rem' }}>
                          The current key is never shown — this only writes a new one. Every node using this channel needs the new key too.
                        </div>
                      </td>
                    </tr>
                  )}
                </React.Fragment>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <div style={{ display: 'flex', gap: '0.5rem', marginTop: '0.75rem', alignItems: 'center', flexWrap: 'wrap' }}>
        <input type="text" placeholder="New channel name (max 10 chars)" value={newName}
               maxLength={10} onChange={e => setNewName(e.target.value)}
               style={{ padding: '0.4rem', width: 220 }}
               onKeyDown={e => e.key === 'Enter' && addChannel()} />
        <label style={{ display: 'flex', alignItems: 'center', gap: '0.3rem', fontSize: '0.85rem' }}>
          <input type="checkbox" checked={useCustomPsk} onChange={e => setUseCustomPsk(e.target.checked)} />
          Set my own key
        </label>
        {useCustomPsk && (
          <input type="text" placeholder="base64 or 0x-hex key" value={newPsk}
                 onChange={e => setNewPsk(e.target.value)} style={{ padding: '0.4rem', width: 260 }} />
        )}
        <button onClick={addChannel} disabled={busy || !newName.trim() || !connected}>Add channel</button>
      </div>
      <div style={{ fontSize: '0.8rem', color: 'var(--muted)', marginTop: '0.5rem' }}>
        {useCustomPsk
          ? 'Paste the key exactly as shown/exported by the official Meshtastic app (16 or 32 bytes, base64 or 0x-hex).'
          : 'A new channel gets a freshly generated encryption key that only exists on this radio — ' +
            'share it (QR code / export) from the official Meshtastic app for other nodes to join it.'}
      </div>
    </div>
  );
}

function LocalInfo() {
  const tempUnit = React.useContext(TempUnitContext);
  const [info, setInfo]   = useState(null);
  const [error, setError] = useState(null);
  const [reqState, setReqState] = useState(null);  // null | 'pending' | 'sent' | string(error)

  const load = useCallback(() => {
    apiFetch('/api/local/info')
      .then(d => { setInfo(d); setError(null); })
      .catch(e => setError(e.message));
  }, []);

  useEffect(() => {
    load();
    const id = setInterval(load, 10000);
    return () => clearInterval(id);
  }, [load]);

  const requestFresh = async (type) => {
    setReqState('pending');
    try {
      const res = await fetch('/api/local/request/telemetry', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ type }),
      });
      const d = await res.json();
      if (!res.ok) throw new Error(d.error || res.statusText);
      setReqState('sent');
      // The radio answers over serial within a couple seconds; reload to show it.
      setTimeout(load, 2500);
      setTimeout(load, 6000);
      setTimeout(() => setReqState(null), 8000);
    } catch (e) {
      setReqState(e.message);
      setTimeout(() => setReqState(null), 6000);
    }
  };

  const fmtUptime = (s) => {
    if (s == null) return '—';
    const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
    return d ? `${d}d ${h}h` : h ? `${h}h ${m}m` : `${m}m`;
  };

  if (!info && !error) return <div className="empty">Loading…</div>;

  const node = (info && info.node) || null;
  const dm   = (info && info.device_metrics) || {};
  const env  = (info && info.environment) || null;
  const secHead = { fontSize: '0.75rem', fontWeight: 600, color: 'var(--muted)', textTransform: 'uppercase', letterSpacing: '0.05em', marginBottom: '0.5rem' };
  const grid    = { display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(150px, 1fr))', gap: '0.75rem', marginBottom: '1.5rem' };

  return (
    <div>
      {error && <div className="error-banner">Error: {error}</div>}
      {info && info.error && <div className="error-banner">Radio error: {info.error}</div>}

      <div style={secHead}>Radio</div>
      <div style={grid}>
        <Card
          label="Connection"
          value={info ? (info.connected ? 'Connected' : 'Disconnected') : '…'}
          accent={info && info.connected ? '#56d364' : '#f85149'}
        />
        <Card label="Serial Port" value={(info && info.serial_port) || '—'} />
        <Card label="Node"     value={node ? (node.long_name || node.short_name || node.node_id) : '—'} />
        <Card label="Node ID"  value={node ? node.node_id : '—'} />
        {node && node.hw_model         && <Card label="Hardware" value={node.hw_model} />}
        {node && node.firmware_version && <Card label="Firmware" value={node.firmware_version} />}
      </div>

      <div style={secHead}>Device</div>
      <div style={grid}>
        <Card label="Battery"  value={dm.battery_level != null ? `${dm.battery_level}%` : '—'}
              accent={dm.battery_level != null ? (dm.battery_level > 50 ? '#56d364' : dm.battery_level > 20 ? '#facc15' : '#f85149') : undefined} />
        <Card label="Voltage"  value={dm.voltage != null ? `${dm.voltage.toFixed(2)}V` : '—'} />
        <Card label="Ch Util"  value={dm.channel_utilization != null ? `${dm.channel_utilization.toFixed(1)}%` : '—'} />
        <Card label="Air TX"   value={dm.air_util_tx != null ? `${dm.air_util_tx.toFixed(1)}%` : '—'} />
        <Card label="Uptime"   value={fmtUptime(dm.uptime_seconds)} />
      </div>

      <div style={{ ...secHead, display: 'flex', alignItems: 'center', gap: '0.75rem' }}>
        <span>Environment</span>
        <button
          className="btn btn-primary btn-sm"
          style={{ textTransform: 'none', letterSpacing: 'normal' }}
          onClick={() => requestFresh('environment_metrics')}
          disabled={reqState === 'pending' || !(info && info.connected)}
        >
          {reqState === 'pending' ? '…' : 'Request fresh reading'}
        </button>
        {reqState === 'sent' && <span style={{ color: '#56d364', textTransform: 'none', letterSpacing: 'normal' }}>Requested — updating shortly</span>}
        {reqState && reqState !== 'sent' && reqState !== 'pending' &&
          <span style={{ color: '#f85149', textTransform: 'none', letterSpacing: 'normal' }}>{reqState}</span>}
      </div>
      {env ? (
        <>
          <div style={grid}>
            {env.temperature         != null && <Card label="Temperature" value={fmtTemp(env.temperature, tempUnit)} accent="#f85149" />}
            {env.relative_humidity   != null && <Card label="Humidity" value={`${env.relative_humidity.toFixed(0)}%`} accent="#58a6ff" />}
            {env.barometric_pressure != null && <Card label="Pressure" value={`${env.barometric_pressure.toFixed(1)} hPa`} accent="#bc8cff" />}
            {env.iaq                 != null && <Card label="IAQ" value={env.iaq} accent="#d2a8ff" />}
            {env.gas_resistance      != null && <Card label="Gas Res" value={`${Math.round(env.gas_resistance)} Ω`} accent="#7ee8a2" />}
          </div>
          <div style={{ fontSize: '0.8rem', color: 'var(--muted)', marginTop: '-0.75rem', marginBottom: '1.5rem' }}>
            {info.environment_source === 'radio'
              ? 'Live from radio node DB'
              : info.environment_received_at
                ? <>As of {fmtTs(info.environment_received_at)} ({fmtFull(info.environment_received_at)}) — click "Request fresh reading" to update</>
                : null}
          </div>
        </>
      ) : (
        <div className="empty" style={{ marginBottom: '1.5rem' }}>
          No environment data yet. If this radio has a sensor (BME280 etc.), click "Request fresh reading".
        </div>
      )}

      <div style={secHead}>Channels</div>
      <DeviceChannels connected={!!(info && info.connected)} />
    </div>
  );
}

// ── Connection status (header dot + sidebar) ────────────────────────────────

// Small always-visible online/offline pill for the header — cheap enough
// (one lightweight in-memory read on the server, see /api/status) to poll
// on every page, not just the Local Node / Diagnostics tabs.
function HeaderStatus() {
  const [online, setOnline] = useState(null);  // null = unknown yet

  useEffect(() => {
    const load = () => apiFetch('/api/status').then(d => setOnline(!!d.serial_connected)).catch(() => setOnline(false));
    load();
    const id = setInterval(load, 10000);
    return () => clearInterval(id);
  }, []);

  const label = online == null ? 'Checking…' : online ? 'Online' : 'Offline';
  return (
    <span className="header-status" title={`Radio ${label}`}>
      <span className={`status-dot ${online ? 'status-online' : 'status-offline'}`} />
      <span className="header-status-label">{label}</span>
    </span>
  );
}

// Desktop-only right rail: connection detail, the most recent packet heard,
// and a rolling 24h summary. Reuses the same endpoints as the Diagnostics
// and Stats tabs so there's no new backend surface.
function StatusSidebar() {
  const [status, setStatus] = useState(null);
  const [stats, setStats]   = useState(null);
  const [lastPacket, setLastPacket] = useState(null);
  const [error, setError]   = useState(null);

  const load = useCallback(() => {
    Promise.all([
      apiFetch('/api/status'),
      apiFetch('/api/stats'),
      apiFetch('/api/packets?limit=1'),
    ]).then(([s, st, pkts]) => {
      setStatus(s);
      setStats(st);
      setLastPacket(pkts[0] || null);
      setError(null);
    }).catch(e => setError(e.message));
  }, []);

  useEffect(() => {
    load();
    const id = setInterval(load, 10000);
    return () => clearInterval(id);
  }, [load]);

  const online = !!(status && status.serial_connected);
  const node = status && status.local_node;
  const nodeLabel = node && (node.long_name || node.short_name || node.node_id);

  return (
    <aside className="status-sidebar no-print">
      <div className="sidebar-card">
        <div className="sidebar-status-row">
          <span className={`status-dot ${online ? 'status-online' : 'status-offline'}`} />
          <span className="sidebar-status-text">{status ? (online ? 'Radio Online' : 'Radio Offline') : 'Checking…'}</span>
        </div>
        {nodeLabel && <div className="sidebar-status-sub">{nodeLabel}{node.hw_model ? ` · ${node.hw_model}` : ''}</div>}
      </div>

      {error && <div className="error-banner">{error}</div>}

      <div className="sidebar-card">
        <div className="sidebar-heading">Last Message</div>
        {lastPacket ? (
          <div>
            <div className="sidebar-last-msg-head">
              {typeBadge(lastPacket.packet_type)}
              <span className="muted" style={{ fontSize: '0.7rem' }}>{fmtTs(lastPacket.received_at)}</span>
            </div>
            <span className="mono sidebar-last-msg-from">{lastPacket.from_node || '—'}</span>
            <div className="sidebar-last-msg-body"><PacketSummary packet={lastPacket} /></div>
          </div>
        ) : <div className="muted" style={{ fontSize: '0.8125rem' }}>No packets yet</div>}
      </div>

      <div className="sidebar-card">
        <div className="sidebar-heading">Last 24h</div>
        {stats && (
          <>
            <div className="sidebar-stats-grid">
              <div>
                <div className="sidebar-stat-value">{stats.packets_1h}</div>
                <div className="sidebar-stat-label">1h</div>
              </div>
              <div>
                <div className="sidebar-stat-value">{stats.packets_24h}</div>
                <div className="sidebar-stat-label">24h</div>
              </div>
              <div>
                <div className="sidebar-stat-value">{stats.active_nodes_24h}</div>
                <div className="sidebar-stat-label">Nodes</div>
              </div>
            </div>
            {stats.by_type_24h.length > 0 && (
              <div className="sidebar-by-type">
                {stats.by_type_24h.slice(0, 6).map(row => (
                  <div key={row.packet_type} className="sidebar-by-type-row">
                    {typeBadge(row.packet_type)}
                    <span className="count">{row.count}</span>
                  </div>
                ))}
              </div>
            )}
          </>
        )}
      </div>
    </aside>
  );
}

// ── App root ──────────────────────────────────────────────────────────────────

const TABS = ['Live Feed', 'Nodes', 'Local Node', 'Messages', 'Map', 'Graphs', 'Report', 'Weather', 'Forwarding Rules', 'Bot', 'Stats', 'Maintenance', 'Diagnostics', 'API'];

function App() {
  const [savedTab, setTab]    = usePersistentState('tab', 'Live Feed');
  const [filters, setFilters] = usePersistentState('filters', { type: '', node: '', channel: '', since: '24h' });
  const [tempUnit, setTempUnit] = useState(() => localStorage.getItem('tempUnit') || 'C');
  const [theme, setTheme] = useState(() => document.documentElement.getAttribute('data-theme') || 'dark');

  // A persisted tab name can go stale if tabs are renamed/removed.
  const tab = TABS.includes(savedTab) ? savedTab : 'Live Feed';

  const showFilter = ['Live Feed', 'Messages'].includes(tab);

  const toggleUnit = () => {
    const next = tempUnit === 'C' ? 'F' : 'C';
    setTempUnit(next);
    localStorage.setItem('tempUnit', next);
  };

  const toggleTheme = () => {
    const next = theme === 'dark' ? 'light' : 'dark';
    setTheme(next);
    document.documentElement.setAttribute('data-theme', next);
    try { localStorage.setItem('theme', next); } catch (_) {}
  };

  return (
    <TempUnitContext.Provider value={tempUnit}>
    <div className="app">
      <header>
        <h1>Mesh<span>tastic</span> Monitor</h1>
        <nav>
          {TABS.map(t => (
            <button key={t} className={tab === t ? 'active' : ''} onClick={() => setTab(t)}>
              {t}
            </button>
          ))}
        </nav>
        <div className="header-controls">
          <HeaderStatus />
          <button className="theme-toggle" onClick={toggleTheme} title="Toggle light / dark theme">
            {theme === 'dark' ? '☀️' : '🌙'}
          </button>
          <button className="unit-toggle" onClick={toggleUnit} title="Toggle temperature unit">
            °{tempUnit}
          </button>
        </div>
      </header>

      <div className="content-shell">
        <div className="main">
          {showFilter && <FilterBar filters={filters} onChange={setFilters} />}

          {tab === 'Live Feed'  && <LiveFeed  filters={filters} />}
          {tab === 'Nodes'      && <Nodes />}
          {tab === 'Local Node' && <LocalInfo />}
          {tab === 'Messages'   && <Messages  filters={filters} />}
          {tab === 'Map'       && <NodeMap />}
          {tab === 'Graphs'    && <Graphs />}
          {tab === 'Report'    && <Report />}
          {tab === 'Weather'   && <Weather />}
          {tab === 'Forwarding Rules' && <Rules />}
          {tab === 'Bot'         && <Bot />}
          {tab === 'Stats'       && <Stats />}
          {tab === 'Maintenance' && <Maintenance />}
          {tab === 'Diagnostics' && <Diagnostics />}
          {tab === 'API'         && <ApiDocs />}
        </div>
        <StatusSidebar />
      </div>
    </div>
    </TempUnitContext.Provider>
  );
}

const root = ReactDOM.createRoot(document.getElementById('root'));
root.render(<App />);
