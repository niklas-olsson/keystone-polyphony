// Opt-in, local-only counters. Never inspect or return application content/keys.
const MAX_CONNECTIONS = 16;
const COUNTERS = ['bytesTransmitted', 'bytesReceived', 'packetsTransmitted', 'packetsReceived'];

function numbers(object, fields) {
  const values = {};
  const unavailable = [];
  for (const field of fields) {
    let value;
    try { value = object && object[field]; } catch (_) { value = null; }
    if (Number.isSafeInteger(value) && value >= 0) values[field] = value;
    else { values[field] = null; unavailable.push(field); }
  }
  return { values, unavailable };
}

function emptyWindow() {
  return {
    write_attempt_frames: 0, write_attempt_bytes: 0,
    write_submitted_frames: 0, write_submitted_bytes: 0,
    write_threw_frames: 0, write_threw_bytes: 0,
    write_backpressure_frames: 0, stream_error_events: 0, receive_bytes: 0,
    min_submitted_frame_bytes: null, max_submitted_frame_bytes: null
  };
}

module.exports = function measurement() {
  const connections = new Map();
  let overflow = false;
  let window = 0;

  function track(conn) {
    if (connections.has(conn)) return;
    if (connections.size >= MAX_CONNECTIONS) { overflow = true; return; }
    const state = { id: connections.size + 1, connected: true, counts: emptyWindow() };
    connections.set(conn, state);
    conn.on('data', (data) => { state.counts.receive_bytes += Buffer.byteLength(data); });
    conn.on('error', () => { state.counts.stream_error_events++; });
    conn.on('close', () => { state.connected = false; });
  }

  function beforeWrite(conn, message) {
    const state = connections.get(conn);
    if (!state) return null;
    const bytes = Buffer.byteLength(message, 'utf8');
    state.counts.write_attempt_frames++;
    state.counts.write_attempt_bytes += bytes;
    return { state, bytes };
  }

  function written(ticket, writable) {
    if (!ticket) return;
    const { counts } = ticket.state;
    counts.write_submitted_frames++;
    counts.write_submitted_bytes += ticket.bytes;
    if (writable === false) counts.write_backpressure_frames++;
    counts.min_submitted_frame_bytes = counts.min_submitted_frame_bytes === null
      ? ticket.bytes : Math.min(counts.min_submitted_frame_bytes, ticket.bytes);
    counts.max_submitted_frame_bytes = counts.max_submitted_frame_bytes === null
      ? ticket.bytes : Math.max(counts.max_submitted_frame_bytes, ticket.bytes);
  }

  function failed(ticket) {
    if (!ticket) return;
    ticket.state.counts.write_threw_frames++;
    ticket.state.counts.write_threw_bytes += ticket.bytes;
  }

  function snapshot() {
    return {
      schema_version: 1, window, overflow,
      connections: [...connections].map(([conn, state]) => {
        let raw = null;
        try { raw = conn.rawStream; } catch (_) { /* unavailable native metadata */ }
        let local = null;
        let remote = null;
        try {
          if (raw && raw.socket) {
            const address = raw.socket.address();
            if (typeof address.host === 'string' && address.host.length <= 64 &&
                Number.isInteger(address.port) && address.port > 0 && address.port <= 65535) {
              local = { host: address.host, port: address.port };
            }
          }
          if (raw && typeof raw.remoteHost === 'string' && raw.remoteHost.length <= 64 &&
              Number.isInteger(raw.remotePort) && raw.remotePort > 0 && raw.remotePort <= 65535) {
            remote = { host: raw.remoteHost, port: raw.remotePort };
          }
        } catch (_) { local = null; remote = null; }
        return {
          connection_id: state.id, connected: state.connected,
          application: { ...state.counts }, endpoints: { local, remote },
          native_stream: numbers(raw, [...COUNTERS, 'retransmits']),
          native_instance: numbers(raw && raw.udx, [...COUNTERS, 'packetsDroppedByKernel'])
        };
      })
    };
  }

  function command(value) {
    if (!value || value.type !== 'local_measurement') return null;
    // An invalid request yields no data and never alters the measurement window.
    if (Object.keys(value).sort().join(',') !== 'operation,request_id,type' ||
        typeof value.request_id !== 'string' || !/^[a-zA-Z0-9._:-]{1,64}$/.test(value.request_id) ||
        !['snapshot', 'reset'].includes(value.operation)) return null;
    if (value.operation === 'reset') {
      window++;
      for (const state of connections.values()) state.counts = emptyWindow();
    }
    return { type: 'local_measurement', request_id: value.request_id, snapshot: snapshot() };
  }

  return { track, beforeWrite, written, failed, command };
};
