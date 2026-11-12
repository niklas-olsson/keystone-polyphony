'use strict';

const { TextDecoder } = require('node:util');

// The delimiter counts toward the limit. Storage is fixed per reader, so a
// fragmented line cannot cause repeated growth or quadratic concatenation.
const MAX_FRAME_BYTES = 65536;

function readBoundedJson(stream, onValue, onInvalid, onEnd = () => {}) {
  const buffer = Buffer.allocUnsafe(MAX_FRAME_BYTES - 1);
  const decoder = new TextDecoder('utf-8', { fatal: true });
  let used = 0;
  let stopped = false;

  function detach() {
    stream.removeListener('data', data);
    stream.removeListener('end', end);
    stream.removeListener('error', error);
  }

  function fail(reason) {
    if (stopped) return;
    stopped = true;
    used = 0;
    detach();
    onInvalid(reason);
  }

  function data(chunk) {
    if (stopped) return;
    // No string-decoding stream may silently repair invalid UTF-8 before us.
    if (!Buffer.isBuffer(chunk)) return fail('non_binary_input');
    let offset = 0;
    while (offset < chunk.length && !stopped) {
      const newline = chunk.indexOf(10, offset);
      const finish = newline === -1 ? chunk.length : newline;
      const length = finish - offset;
      if (used + length >= MAX_FRAME_BYTES) return fail('frame_too_large');
      chunk.copy(buffer, used, offset, finish);
      used += length;
      if (newline === -1) break;
      offset = newline + 1;
      if (!used) continue;
      let value;
      try {
        const line = decoder.decode(buffer.subarray(0, used));
        value = JSON.parse(line);
        if (value === null || typeof value !== 'object' || Array.isArray(value)) {
          return fail('invalid_object');
        }
      } catch (_) {
        return fail('invalid_json_or_utf8');
      }
      used = 0;
      try {
        onValue(value);
      } catch (_) {
        return fail('invalid_frame');
      }
    }
  }

  function end() {
    if (stopped) return;
    if (used) return fail('unterminated_frame');
    stopped = true;
    detach();
    onEnd();
  }

  function error() {
    fail('stream_error');
  }

  stream.on('data', data);
  stream.on('end', end);
  stream.on('error', error);
  return () => {
    stopped = true;
    used = 0;
    detach();
  };
}

function isEnvelope(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value) &&
    Object.keys(value).sort().join(',') === 'e,p,s' &&
    typeof value.e === 'string' && /^[A-Za-z0-9_-]+={0,2}$/.test(value.e) &&
    typeof value.p === 'string' && /^[0-9a-f]{64}$/.test(value.p) &&
    typeof value.s === 'string' && /^[0-9a-f]{128}$/.test(value.s);
}

// streamx/SecretStream exposes boolean write/drain, not Node write callbacks.
// Count our queue directly. The stream may additionally own its high-water
// buffer and one accepted frame; this is not a whole-process memory bound.
class BoundedWriter {
  constructor(stream, maximum, onFailure) {
    this.stream = stream;
    this.maximum = maximum;
    this.onFailure = onFailure;
    this.queue = [];
    this.bytes = 0;
    this.blocked = false;
    this.closed = false;
    this.drain = () => {
      this.blocked = false;
      this.pump();
    };
    stream.on('drain', this.drain);
  }

  enqueue(raw) {
    if (this.closed || !Buffer.isBuffer(raw) || raw.length > MAX_FRAME_BYTES ||
        this.bytes + raw.length > this.maximum) return false;
    this.queue.push(raw);
    this.bytes += raw.length;
    this.pump();
    return !this.closed;
  }

  pump() {
    if (this.closed || this.blocked) return;
    try {
      while (this.queue.length && !this.closed) {
        const raw = this.queue.shift();
        this.bytes -= raw.length;
        if (this.stream.write(raw) === false) {
          this.blocked = true;
          break;
        }
      }
    } catch (_) {
      this.close();
      this.onFailure();
    }
  }

  close() {
    if (this.closed) return;
    this.closed = true;
    this.stream.removeListener('drain', this.drain);
    this.queue.length = 0;
    this.bytes = 0;
  }
}

function restrictedArguments(argv) {
  const args = argv.slice(2);
  if ((args.length !== 1 && args.length !== 3) || !/^[0-9a-f]{64}$/.test(args[0])) {
    throw new Error('Invalid restricted transport configuration');
  }
  let bootstrap;
  if (args.length === 3) {
    if (args[1] !== '--conversation-bootstrap-port' ||
        !/^[1-9][0-9]{0,4}$/.test(args[2]) || Number(args[2]) > 65535) {
      throw new Error('Invalid restricted transport configuration');
    }
    bootstrap = [{ host: '127.0.0.1', port: Number(args[2]) }];
  }
  return { topic: args[0], bootstrap };
}

function runRestricted({ Hyperswarm, HyperDHT, b4a, runtime = process,
                         setTimer = setTimeout, clearTimer = clearTimeout }) {
  let settings;
  try {
    settings = restrictedArguments(runtime.argv);
  } catch (_) {
    runtime.stderr.write('Restricted conversation configuration refused.\n');
    runtime.exit(1);
    return;
  }
  const peers = new Map();
  let closing = false;
  let stopInput = () => {};
  let output;
  let swarm;

  async function close(code) {
    if (closing) return;
    closing = true;
    stopInput();
    runtime.stdin.pause();
    for (const peer of [...peers.values()]) peer.finish();
    if (output) output.close();
    const timer = setTimer(() => runtime.exit(1), 1500);
    if (timer.unref) timer.unref();
    try {
      if (swarm) await swarm.destroy();
    } catch (_) {
      code = 1;
    } finally {
      clearTimer(timer);
    }
    runtime.exit(code);
  }

  try {
    if (settings.bootstrap) {
      const DHT = HyperDHT || require('hyperdht');
      const dht = new DHT({ bootstrap: settings.bootstrap, nodes: [], host: '127.0.0.1' });
      swarm = new Hyperswarm({ dht });
    } else {
      swarm = new Hyperswarm();
    }
  } catch (_) {
    close(1);
    return;
  }
  output = new BoundedWriter(runtime.stdout, 4 * MAX_FRAME_BYTES, () => close(1));
  const outputError = () => close(1);
  runtime.stdout.on('error', outputError);

  function emit(value, control = true) {
    if (closing) return false;
    const raw = Buffer.from(JSON.stringify(value) + '\n');
    if (!output.enqueue(raw)) {
      if (control) close(1);
      return false;
    }
    return true;
  }

  swarm.on('connection', (conn, info) => {
    // Keep one inert error observer after finalization: closed streamx streams
    // may still emit a late error. All stateful owned listeners are detached.
    conn.on('error', () => {});
    const peerId = info && info.publicKey && b4a.toString(info.publicKey, 'hex');
    if (closing || !/^[0-9a-f]{64}$/.test(peerId || '') ||
        (!peers.has(peerId) && peers.size >= 64)) {
      conn.destroy();
      return;
    }
    const old = peers.get(peerId);
    let ended = false;
    let detach = () => {};
    const writer = new BoundedWriter(conn, 2 * MAX_FRAME_BYTES, () => finish());
    const peer = { writer, finish };

    function finish() {
      if (ended) return;
      ended = true;
      detach();
      writer.close();
      conn.removeListener('close', finish);
      conn.removeListener('error', finish);
      if (peers.get(peerId) === peer) {
        peers.delete(peerId);
        if (!closing) emit({ type: 'peer_disconnected', peer_id: peerId });
      }
      conn.destroy();
    }

    peers.set(peerId, peer);
    if (old) old.finish();
    conn.on('close', finish);
    conn.on('error', finish);
    detach = readBoundedJson(conn, message => {
      if (ended || closing || peers.get(peerId) !== peer) return;
      if (!isEnvelope(message)) throw new Error('Invalid restricted envelope');
      if (!emit({ type: 'message', peer_id: peerId, payload: message }, false)) finish();
    }, finish, finish);
    emit({ type: 'peer_connected', peer_id: peerId });
  });

  stopInput = readBoundedJson(runtime.stdin, command => {
    if (closing) return;
    if (Object.keys(command).sort().join(',') !== 'payload,type' ||
        command.type !== 'broadcast' || !isEnvelope(command.payload)) {
      throw new Error('Invalid restricted command');
    }
    const raw = Buffer.from(JSON.stringify(command.payload) + '\n');
    if (raw.length > MAX_FRAME_BYTES) throw new Error('Oversized restricted command');
    for (const peer of [...peers.values()]) {
      if (!peer.writer.enqueue(raw)) peer.finish();
    }
  }, () => close(1), () => close(0));

  runtime.on('SIGTERM', () => close(0));
  runtime.on('SIGINT', () => close(0));
  runtime.on('uncaughtException', () => close(1));
  runtime.on('unhandledRejection', () => close(1));
  try {
    const discovery = swarm.join(b4a.from(settings.topic, 'hex'), { client: true, server: true });
    Promise.resolve(discovery.flushed()).then(() => {
      emit({ type: 'conversation_ready', topic: settings.topic,
             profile: 'polyphony-conversation-only/v1' });
    }, () => close(1));
  } catch (_) {
    close(1);
  }
  // This handle is for deterministic ownership/stream tests, never a wire API.
  return { peers, output, close };
}

module.exports = { MAX_FRAME_BYTES, readBoundedJson, isEnvelope,
                   BoundedWriter, restrictedArguments, runRestricted };
