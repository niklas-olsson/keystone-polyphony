// Pure diagnostics/bridge control tests. No claim of real native transport.
const test = require('node:test');
const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const makeMeasurement = require('../src/liminal_bridge/sidecar/measurement');

function stream() {
  const conn = new EventEmitter();
  conn.rawStream = {
    bytesTransmitted: 40, bytesReceived: 50, packetsTransmitted: 2, packetsReceived: 3, retransmits: 0,
    remoteHost: '127.0.0.1', remotePort: 3001,
    socket: { address: () => ({ host: '0.0.0.0', port: 3000 }) },
    udx: { bytesTransmitted: 100, bytesReceived: 200, packetsTransmitted: 4, packetsReceived: 8, packetsDroppedByKernel: 0 }
  };
  conn.writes = [];
  conn.write = (message) => { conn.writes.push(message); return false; };
  return conn;
}

function sample(measurement, operation = 'snapshot') {
  return measurement.command({ type: 'local_measurement', request_id: 'request-1', operation }).snapshot;
}

test('exact UTF-8 framing, backpressure, write failure and interval extrema are separate', () => {
  const measurement = makeMeasurement();
  const conn = stream();
  measurement.track(conn);
  measurement.written(measurement.beforeWrite(conn, 'å\n'), false);
  measurement.failed(measurement.beforeWrite(conn, 'larger failed frame\n'));
  conn.emit('data', Buffer.from('å\n'));
  conn.emit('error', new Error('private error message must not be emitted'));
  const first = sample(measurement).connections[0];
  assert.equal(first.application.write_attempt_frames, 2);
  assert.equal(first.application.write_submitted_bytes, 3);
  assert.equal(first.application.write_backpressure_frames, 1);
  assert.equal(first.application.write_threw_frames, 1);
  assert.equal(first.application.receive_bytes, 3);
  assert.equal(first.application.min_submitted_frame_bytes, 3);
  assert.equal(first.application.max_submitted_frame_bytes, 3);
  assert.equal(first.application.stream_error_events, 1);
  const reset = sample(measurement, 'reset');
  assert.equal(reset.window, 1);
  assert.equal(reset.connections[0].application.min_submitted_frame_bytes, null);
  measurement.written(measurement.beforeWrite(conn, 'x\n'), true);
  assert.equal(sample(measurement).connections[0].application.max_submitted_frame_bytes, 2);
  assert.equal(sample(measurement).connections[0].native_stream.values.bytesTransmitted, 40);
  assert.equal(sample(measurement).connections[0].native_instance.values.bytesTransmitted, 100);
  assert(!JSON.stringify(sample(measurement)).includes('private'));
});

test('invalid requests do not reset; absent or unsafe native values are never zero', () => {
  const measurement = makeMeasurement();
  const conn = stream();
  conn.rawStream.bytesReceived = Number.MAX_SAFE_INTEGER + 1;
  delete conn.rawStream.udx;
  measurement.track(conn);
  for (const changed of [{ operation: 'dump' }, { request_id: 'x'.repeat(65) }, { payload: {} }, { request_id: null }]) {
    assert.equal(measurement.command({ type: 'local_measurement', request_id: 'valid', operation: 'reset', ...changed }), null);
  }
  const value = sample(measurement);
  assert.equal(value.window, 0);
  assert.equal(value.connections[0].native_stream.values.bytesReceived, null);
  assert.deepEqual(value.connections[0].native_stream.unavailable, ['bytesReceived']);
  assert(Object.values(value.connections[0].native_instance.values).every(item => item === null));
  conn.emit('close');
  assert.equal(sample(measurement).connections[0].connected, false);
  for (let index = 0; index < 20; index++) measurement.track(stream());
  assert.equal(sample(measurement).connections.length, 16);
  assert.equal(sample(measurement).overflow, true);
});

function bridge(enabled, settings = {}) {
  const readers = [];
  const outputs = [];
  const trace = settings.trace || {};
  Object.assign(trace, { dhts: [], swarms: [], handlers: {}, dhtDestroy: 0, swarmDestroy: 0 });
  const keyPair = { publicKey: Buffer.from('stable-public'), secretKey: Buffer.from('stable-private') };
  let instance;
  let diagnosticsLoads = 0;
  class DHT {
    constructor(options) { this.options = options; trace.dhts.push(this); }
    async destroy() { trace.dhtDestroy++; }
  }
  class Swarm extends EventEmitter {
    constructor(options) {
      super();
      this.options = options;
      trace.swarms.push(this);
      if (settings.failSwarm) throw new Error('controlled swarm construction failure');
      instance = this;
    }
    join() { return { flushed: async () => {}, destroy: async () => {} }; }
    async destroy() { trace.swarmDestroy++; if (this.options.dht) await this.options.dht.destroy(); }
  }
  const context = {
    Buffer,
    process: {
      argv: ['node', 'bridge.js', 'aa'.repeat(32), ...(settings.args || [])],
      env: { ...(enabled ? { POLYPHONY_LOCAL_MEASUREMENT: '1' } : {}), ...settings.env },
      stdout: { write: value => outputs.push(JSON.parse(value)) }, stderr: { write: () => {} },
      stdin: {}, on: (name, handler) => { trace.handlers[name] = handler; }, removeAllListeners: () => {},
      exit: code => { throw Object.assign(new Error('controlled process exit'), { code }); }
    },
    require: name => {
      if (name === 'hyperswarm') return Swarm;
      if (name === 'hyperdht') return DHT;
      if (name === 'hypercore-crypto') return { keyPair: () => keyPair };
      if (name === 'b4a') return { from: Buffer.from, toString: (value, encoding) => value.toString(encoding) };
      if (name === 'readline') return { createInterface: () => { const reader = new EventEmitter(); readers.push(reader); return reader; } };
      if (name === './measurement') { diagnosticsLoads++; return makeMeasurement; }
      throw new Error('unexpected module ' + name);
    }
  };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../src/liminal_bridge/sidecar/bridge.js'), 'utf8'), context);
  const conn = stream();
  instance.emit('connection', conn, { publicKey: Buffer.from('fixture') });
  return { conn, outputs, readers, diagnosticsLoads, trace, keyPair };
}

test('actual bridge ignores diagnostics when disabled and preserves ordinary writes', () => {
  const value = bridge(false);
  value.readers[0].emit('line', JSON.stringify({ type: 'local_measurement', request_id: 'request', operation: 'reset' }));
  value.readers[0].emit('line', JSON.stringify({ type: 'broadcast', payload: { x: 'å' } }));
  assert.equal(value.diagnosticsLoads, 0);
  assert.deepEqual(value.conn.writes, ['{"x":"å"}\n']);
  assert(!value.outputs.some(message => message.type === 'local_measurement'));
});

test('actual bridge local diagnostics never travel to peers or dispatch peer payloads', () => {
  const value = bridge(true);
  value.readers[0].emit('line', JSON.stringify({ type: 'broadcast', payload: { x: 'å' } }));
  const command = { type: 'local_measurement', request_id: 'request', operation: 'reset' };
  value.readers[1].emit('line', JSON.stringify(command));
  assert(!value.outputs.some(message => message.type === 'local_measurement'));
  assert(value.outputs.some(message => message.type === 'message' && message.payload.type === 'local_measurement'));
  value.readers[0].emit('line', JSON.stringify(command));
  assert.equal(value.outputs.filter(message => message.type === 'local_measurement').length, 1);
  assert.deepEqual(value.conn.writes, ['{"x":"å"}\n']);
  assert.equal(value.outputs.find(message => message.type === 'local_measurement').snapshot.connections[0].application.write_submitted_frames, 0);
});

test('ordinary and diagnostics-only transport options retain the original constructor and seed', () => {
  for (const enabled of [false, true]) {
    for (const args of [[], ['--bootstrap', '192.0.2.1:2345', '--seed', 'ab'.repeat(32)]]) {
      const value = bridge(enabled, { args });
      assert.equal(value.trace.dhts.length, 0);
      const options = value.trace.swarms[0].options;
      assert.deepEqual(Object.keys(options).sort(), ['bootstrap', 'keyPair']);
      if (args.length) {
        assert.deepEqual(JSON.parse(JSON.stringify(options.bootstrap)), [{ host: '192.0.2.1', port: 2345 }]);
        assert.equal(options.keyPair, value.keyPair);
      } else {
        assert.equal(options.bootstrap, undefined);
        assert.equal(options.keyPair, undefined);
      }
    }
  }
});

test('measurement opt-in injects exact loopback sockets without changing swarm identity or shutdown ownership', async () => {
  const value = bridge(true, {
    env: { POLYPHONY_MEASUREMENT_LOOPBACK: '1' },
    args: ['--bootstrap', '127.0.0.1:12345', '--seed', 'ab'.repeat(32)]
  });
  const { trace } = value;
  assert.equal(trace.dhts.length, 1);
  assert.deepEqual(JSON.parse(JSON.stringify(trace.dhts[0].options)), {
    bootstrap: [{ host: '127.0.0.1', port: 12345 }], nodes: [], host: '127.0.0.1'
  });
  assert.deepEqual(Object.keys(trace.swarms[0].options).sort(), ['dht', 'keyPair']);
  assert.equal(trace.swarms[0].options.dht, trace.dhts[0]);
  assert.equal(trace.swarms[0].options.keyPair, value.keyPair);
  value.readers[0].emit('line', JSON.stringify({ type: 'broadcast', payload: { x: 'å' } }));
  assert.deepEqual(value.conn.writes, ['{"x":"å"}\n']);
  assert.equal(trace.dhtDestroy, 0);
  await assert.rejects(trace.handlers.SIGINT(), error => error.code === 0);
  assert.equal(trace.swarmDestroy, 1);
  assert.equal(trace.dhtDestroy, 1);
});

test('invalid measurement opt-in and bootstrap fail before any DHT or swarm construction', () => {
  const cases = [
    { enabled: false, args: ['--bootstrap', '127.0.0.1:12345'] },
    { flag: '0', args: ['--bootstrap', '127.0.0.1:12345'] },
    { flag: 'true', args: ['--bootstrap', '127.0.0.1:12345'] },
    { args: [] }, { args: ['--bootstrap'] },
    { args: ['--bootstrap', '127.0.0.1:12345', '--bootstrap', '127.0.0.1:12345'] },
    ...['localhost:12345', '192.0.2.1:12345', '127.0.0.2:12345', '127.0.0.1:0',
      '127.0.0.1:-1', '127.0.0.1:65536', '127.0.0.1:0123', '127.0.0.1:123x',
      '127.0.0.1:123:456', '127.0.0.1:123\n'].map(address => ({ args: ['--bootstrap', address] }))
  ];
  for (const entry of cases) {
    const trace = {};
    assert.throws(() => bridge(entry.enabled !== false, {
      trace, args: entry.args, env: { POLYPHONY_MEASUREMENT_LOOPBACK: entry.flag || '1' }
    }), error => error.code === 1);
    assert.equal(trace.dhts.length, 0);
    assert.equal(trace.swarms.length, 0);
  }
});

test('injected transport construction failure cannot be swallowed or fall back to ordinary networking', () => {
  const trace = {};
  assert.throws(() => bridge(true, {
    trace, failSwarm: true, env: { POLYPHONY_MEASUREMENT_LOOPBACK: '1' },
    args: ['--bootstrap', '127.0.0.1:12345']
  }), /controlled swarm construction failure/);
  assert.equal(trace.dhts.length, 1);
  assert.equal(trace.swarms.length, 1);
  assert.equal(trace.handlers.uncaughtException, undefined);
  assert.equal(trace.handlers.unhandledRejection, undefined);
});

test('actual bootstrap helper binds loopback, awaits readiness and destroys its node on SIGTERM', async () => {
  const calls = [], handlers = {}, output = [];
  let ready;
  const node = {
    ready: () => new Promise(resolve => { ready = resolve; }),
    destroy: async options => calls.push(['destroy', options])
  };
  const context = {
    process: {
      stdout: { write: value => output.push(value) },
      on: (name, handler) => { handlers[name] = handler; },
      exit: code => calls.push(['exit', code])
    },
    console: { error: error => { throw error; } },
    require: name => {
      if (name === 'hyperdht') return { bootstrapper: (...args) => { calls.push(['bootstrapper', ...args]); return node; } };
      if (name === 'dgram') return { createSocket: family => ({
        once: () => {},
        bind: (port, host, done) => { calls.push(['bind', family, port, host]); done(); },
        address: () => ({ port: 23456 }), close: done => done()
      }) };
      throw new Error('unexpected module ' + name);
    }
  };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../src/liminal_bridge/sidecar/verify-bootstrap.js'), 'utf8'), context);
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(output, []);
  assert.deepEqual(JSON.parse(JSON.stringify(calls)), [
    ['bind', 'udp4', 0, '127.0.0.1'],
    ['bootstrapper', 23456, '127.0.0.1', { host: '127.0.0.1', nodes: [] }]
  ]);
  ready();
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(output, ['127.0.0.1:23456\n']);
  await handlers.SIGTERM();
  assert.deepEqual(JSON.parse(JSON.stringify(calls.slice(-2))), [['destroy', { force: true }], ['exit', 0]]);
});
