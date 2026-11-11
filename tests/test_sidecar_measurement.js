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

function bridge(enabled) {
  const readers = [];
  const outputs = [];
  let instance;
  let diagnosticsLoads = 0;
  class Swarm extends EventEmitter {
    constructor() { super(); instance = this; }
    join() { return { flushed: async () => {}, destroy: async () => {} }; }
    async destroy() {}
  }
  const context = {
    Buffer,
    process: {
      argv: ['node', 'bridge.js', 'aa'.repeat(32)], env: enabled ? { POLYPHONY_LOCAL_MEASUREMENT: '1' } : {},
      stdout: { write: value => outputs.push(JSON.parse(value)) }, stderr: { write: () => {} },
      stdin: {}, on: () => {}, removeAllListeners: () => {}, exit: () => { throw new Error('unexpected exit'); }
    },
    require: name => {
      if (name === 'hyperswarm') return Swarm;
      if (name === 'hypercore-crypto') return {};
      if (name === 'b4a') return { from: Buffer.from, toString: (value, encoding) => value.toString(encoding) };
      if (name === 'readline') return { createInterface: () => { const reader = new EventEmitter(); readers.push(reader); return reader; } };
      if (name === './measurement') { diagnosticsLoads++; return makeMeasurement; }
      throw new Error('unexpected module ' + name);
    }
  };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../src/liminal_bridge/sidecar/bridge.js'), 'utf8'), context);
  const conn = stream();
  instance.emit('connection', conn, { publicKey: Buffer.from('fixture') });
  return { conn, outputs, readers, diagnosticsLoads };
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
