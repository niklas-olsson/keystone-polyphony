'use strict';

// Actual Node/streamx streams; discovery and process exit are explicit fixtures.
// These cases do not establish native peer delivery.
const test = require('node:test');
const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const { PassThrough, Writable } = require('node:stream');
const { Duplex } = require('../src/liminal_bridge/sidecar/node_modules/streamx');
const framing = require('../src/liminal_bridge/sidecar/conversation-framing');
const turn = () => new Promise(resolve => setImmediate(resolve));
const envelope = (size = 8) => ({ e: 'x'.repeat(size), p: 'ab'.repeat(32), s: 'cd'.repeat(64) });

class Peer extends Duplex {
  constructor(blocked = false) {
    super({ mapWritable: value => Buffer.isBuffer(value) ? value : Buffer.from(value) });
    this.blocked = blocked;
    this.pending = null;
    this.calls = 0;
    this.writes = [];
  }
  _read(done) { done(null); }
  _write(chunk, done) {
    this.writes.push(Buffer.from(chunk));
    if (this.blocked) this.pending = done;
    else done(null);
  }
  write(value) { this.calls++; return super.write(value); }
  _predestroy() { if (this.pending) { const done = this.pending; this.pending = null; done(null); } }
  receive(value) { this.push(Buffer.isBuffer(value) ? value : Buffer.from(value)); }
  release() {
    this.blocked = false;
    if (this.pending) { const done = this.pending; this.pending = null; done(null); }
  }
}

function runtime(options = {}) {
  const input = new PassThrough();
  const outputs = [], exits = [], constructors = [], timers = [];
  let blockedWrite;
  const output = new Writable({ highWaterMark: 16384, write(chunk, encoding, done) {
    outputs.push(Buffer.from(chunk));
    if (options.blockedOutput) blockedWrite = done;
    else done();
  } });
  const process = Object.assign(new EventEmitter(), {
    argv: ['node', 'bridge.js', 'ef'.repeat(32), ...(options.args || [])],
    stdin: input, stdout: output, stderr: { write: () => true },
    exit: code => exits.push(code),
  });
  let resolve, reject, swarm;
  const ready = new Promise((yes, no) => { resolve = yes; reject = no; });
  class Swarm extends EventEmitter {
    constructor(settings) { super(); swarm = this; constructors.push(['swarm', settings]); }
    join(topic, settings) {
      constructors.push(['join', topic.toString('hex'), settings]);
      return { flushed: () => ready };
    }
    async destroy() {
      if (options.hungDestroy) await new Promise(() => {});
      if (options.failedDestroy) throw new Error('declared teardown fixture');
    }
  }
  class DHT { constructor(settings) { constructors.push(['dht', settings]); } }
  const handle = framing.runRestricted({ Hyperswarm: Swarm, HyperDHT: DHT,
    b4a: { from: Buffer.from, toString: (value, encoding) => value.toString(encoding) },
    runtime: process, setTimer: (callback, delay) => {
      const timer = { callback, delay, unref() {} }; timers.push(timer); return timer;
    }, clearTimer: timer => { timer.cleared = true; },
  });
  return { input, output, outputs, exits, constructors, timers, process, handle,
    resolve, reject, connect(peer, id = 'ab'.repeat(32)) {
      swarm.emit('connection', peer, { publicKey: Buffer.from(id, 'hex') });
    }, events() { return outputs.map(raw => JSON.parse(String(raw))); },
    releaseOutput() { options.blockedOutput = false; if (blockedWrite) blockedWrite(); },
  };
}

test('fixed reader supports fragmented UTF-8 and coalesced frames', async () => {
  const input = new PassThrough();
  const values = [], errors = [];
  framing.readBoundedJson(input, value => values.push(value), error => errors.push(error));
  for (const byte of Buffer.from('{"message":"å🙂"}\n')) input.write(Buffer.from([byte]));
  input.end(Buffer.from('{"n":1}\n{"n":2}\n'));
  await turn();
  assert.deepEqual(values, [{ message: 'å🙂' }, { n: 1 }, { n: 2 }]);
  assert.deepEqual(errors, []);
});

test('exact frame limit succeeds, overflow and invalid input stop only that reader', async () => {
  const line = Buffer.from(JSON.stringify({ x: 'x'.repeat(framing.MAX_FRAME_BYTES - 9) }) + '\n');
  assert.equal(line.length, framing.MAX_FRAME_BYTES);
  for (const [raw, valid] of [[line, true], [Buffer.alloc(framing.MAX_FRAME_BYTES, 120), false],
    [Buffer.from([123, 34, 120, 34, 58, 34, 255, 34, 125, 10]), false],
    [Buffer.from('null\n'), false], [Buffer.from('[]\n'), false],
    [Buffer.from('{bad}\n'), false], [Buffer.from('{"partial":1}'), false]]) {
    const input = new PassThrough(), values = [], errors = [];
    framing.readBoundedJson(input, value => values.push(value), error => errors.push(error));
    input.end(raw);
    await turn();
    assert.equal(values.length, valid ? 1 : 0);
    assert.equal(errors.length, valid ? 0 : 1);
    assert.equal(input.listenerCount('data'), 0);
  }
});

test('real streamx write(false) stops all further writes until drain', async () => {
  const peer = new Peer(true);
  const writer = new framing.BoundedWriter(peer, 131072, () => assert.fail('unexpected stream failure'));
  const raw = Buffer.alloc(60000, 120);
  assert.equal(writer.enqueue(raw), true);
  assert.equal(writer.enqueue(raw), true);
  assert.equal(writer.enqueue(raw), true);
  assert.equal(writer.enqueue(raw), false);
  await turn();
  assert.equal(peer.calls, 1);
  assert.equal(writer.bytes, 120000);
  peer.release();
  await turn();
  await turn();
  assert.equal(peer.calls, 3);
  assert.equal(writer.bytes, 0);
  writer.close();
  assert.equal(peer.listenerCount('drain'), 0);
  assert.equal(writer.enqueue(raw), false);
  peer.destroy();
});

test('slow peer is detached while healthy peer receives all valid broadcasts', async () => {
  const value = runtime();
  const slow = new Peer(true), healthy = new Peer();
  value.connect(slow);
  value.connect(healthy, 'cd'.repeat(32));
  value.resolve();
  await turn();
  for (let count = 0; count < 24; count++) {
    value.input.write(JSON.stringify({ type: 'broadcast', payload: envelope(60000) }) + '\n');
    await turn();
  }
  assert.equal(slow.calls, 1);
  assert.equal(value.handle.peers.size, 1);
  assert.equal(healthy.writes.length, 24);
  assert.deepEqual(value.exits, []);
  await value.handle.close(0);
});

test('blocked stdout stays bounded and closes its offending peer', async () => {
  const value = runtime({ blockedOutput: true });
  const peer = new Peer();
  value.resolve();
  value.connect(peer);
  await turn();
  for (let count = 0; count < 24; count++) {
    peer.receive(JSON.stringify(envelope(60000)) + '\n');
    await turn();
  }
  assert.ok(value.handle.output.bytes <= 262144);
  assert.ok(value.output.writableLength <= 65536);
  assert.equal(value.handle.peers.size, 0);
  value.releaseOutput();
  await turn();
  await value.handle.close(0);
});

test('stale replacement callbacks cannot remove or forward the new connection', async () => {
  const value = runtime();
  const old = new Peer(), replacement = new Peer();
  value.connect(old);
  value.connect(replacement);
  old.emit('close');
  old.emit('error', new Error('stale fixture error'));
  old.emit('data', Buffer.from(JSON.stringify(envelope(9)) + '\n'));
  replacement.receive(JSON.stringify(envelope(10)) + '\n');
  await turn();
  assert.equal(value.handle.peers.size, 1);
  assert.equal(value.events().filter(item => item.type === 'peer_disconnected').length, 0);
  assert.equal(value.events().filter(item => item.type === 'message').length, 1);
  assert.equal(value.events().find(item => item.type === 'message').payload.e.length, 10);
  assert.equal(old.listenerCount('data'), 0);
  assert.equal(old.listenerCount('drain'), 0);
  replacement.emit('error', new Error('current fixture error'));
  replacement.emit('close');
  assert.equal(value.events().filter(item => item.type === 'peer_disconnected').length, 1);
  await value.handle.close(0);
});

test('malformed peer cannot prevent a separate valid peer from delivering', async () => {
  const value = runtime();
  const bad = new Peer(), good = new Peer();
  value.connect(bad);
  value.connect(good, 'cd'.repeat(32));
  bad.receive(Buffer.from([255, 10]));
  good.receive(JSON.stringify(envelope()) + '\n');
  await turn();
  assert.equal(value.handle.peers.size, 1);
  assert.equal(value.events().filter(item => item.type === 'message').length, 1);
  await value.handle.close(0);
});

test('readiness follows flush and control overflow is a visible terminal failure', async () => {
  const value = runtime();
  assert.deepEqual(value.events(), []);
  value.resolve();
  await turn();
  assert.deepEqual(value.events(), [{ type: 'conversation_ready', topic: 'ef'.repeat(32),
    profile: 'polyphony-conversation-only/v1' }]);
  await value.handle.close(0);
  const blocked = runtime({ blockedOutput: true });
  blocked.output.write(Buffer.alloc(16384));
  blocked.handle.output.blocked = true;
  for (let i = 0; i < 4; i++) assert.equal(blocked.handle.output.enqueue(Buffer.alloc(65536)), true);
  blocked.resolve();
  await turn();
  assert.deepEqual(blocked.exits, [1]);
});

test('restricted configuration uses exactly one loopback DHT and rejects aliases/duplicates before transport', async () => {
  const value = runtime({ args: ['--conversation-bootstrap-port', '12345'] });
  assert.deepEqual(value.constructors[0], ['dht', {
    bootstrap: [{ host: '127.0.0.1', port: 12345 }], nodes: [], host: '127.0.0.1',
  }]);
  assert.deepEqual(Object.keys(value.constructors[1][1]), ['dht']);
  await value.handle.close(0);
  for (const args of [['--bootstrap', '127.0.0.1:12345'], ['--conversation-bootstrap-port', '0'],
    ['--conversation-bootstrap-port', '65536'], ['--conversation-bootstrap-port', '1.0'],
    ['--conversation-bootstrap-port', '12', '--conversation-bootstrap-port', '12']]) {
    const refused = runtime({ args });
    assert.deepEqual(refused.constructors, []);
    assert.deepEqual(refused.exits, [1]);
  }
});

test('64 active peers and no local topic/measurement command escape', async () => {
  const value = runtime();
  for (let index = 0; index < 65; index++) value.connect(new Peer(), index.toString(16).padStart(64, '0'));
  assert.equal(value.handle.peers.size, 64);
  value.input.write(JSON.stringify({ type: 'local_measurement', payload: {} }) + '\n');
  await turn();
  assert.deepEqual(value.exits, [1]);
  assert.equal(value.handle.peers.size, 0);
});

test('EOF, signals, rejected flush and hung shutdown have bounded termination', async () => {
  const eof = runtime(); eof.input.end(); await turn(); assert.deepEqual(eof.exits, [0]);
  const signal = runtime(); signal.process.emit('SIGTERM'); await turn(); assert.deepEqual(signal.exits, [0]);
  const flush = runtime(); flush.reject(new Error('fixture discovery failure')); await turn(); assert.deepEqual(flush.exits, [1]);
  const hung = runtime({ hungDestroy: true }); hung.process.emit('SIGTERM');
  assert.equal(hung.timers[0].delay, 1500);
  hung.timers[0].callback();
  assert.deepEqual(hung.exits, [1]);
});
