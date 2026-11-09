// An isolated, loopback-only bootstrap node for the two-node verification.
const DHT = require('hyperdht');
const dgram = require('dgram');

async function main() {
  const socket = dgram.createSocket('udp4');
  await new Promise((resolve, reject) => {
    socket.once('error', reject);
    socket.bind(0, '127.0.0.1', resolve);
  });
  const { port } = socket.address();
  await new Promise(resolve => socket.close(resolve));
  const node = DHT.bootstrapper(port, '127.0.0.1');
  await node.ready();
  process.stdout.write(`127.0.0.1:${port}\n`);
  process.on('SIGTERM', async () => {
    await node.destroy({ force: true });
    process.exit(0);
  });
}

main().catch(err => {
  console.error(err);
  process.exit(1);
});
