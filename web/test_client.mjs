// Runs the phone page's Client (web/protocol.js) against the real Python device over the
// simulation socket, the way the page does it over Bluetooth: a wrong code, the right code,
// a scan, a wrong password, then the right one. The link here is the sim wire format
// (teton_common/sim_wire.py: [u8 channel][u16 big-endian length][chunk]) instead of GATT.
//
// Run by tests/test_web.py, which starts the device:
//   node --no-warnings web/test_client.mjs <socket path> <setup code> <wrong code>

import assert from "node:assert/strict";
import { webcrypto } from "node:crypto";
import net from "node:net";

globalThis.crypto ??= webcrypto;
const P = await import("./protocol.js");
const [socketPath, code, wrongCode] = process.argv.slice(2);

const CHANNELS = { info: 0, session: 1, command: 2, status: 3 };
const NAMES = { 1: "session", 3: "status" };

const socket = net.createConnection(socketPath);
await new Promise((resolve, reject) => socket.once("connect", resolve).once("error", reject));

const record = (channel, chunk) => {
  const header = Buffer.alloc(3);
  header[0] = CHANNELS[channel];
  header.writeUInt16BE(chunk.length, 1);
  return Buffer.concat([header, Buffer.from(chunk)]);
};
const send = (channel, chunk) =>
  new Promise((resolve, reject) =>
    socket.write(record(channel, chunk), (err) => (err ? reject(err) : resolve())));

const client = new P.Client(send);
let infoWaiter = null;
let buffer = Buffer.alloc(0);
socket.on("data", (data) => {
  buffer = Buffer.concat([buffer, data]);
  while (buffer.length >= 3 && buffer.length >= 3 + buffer.readUInt16BE(1)) {
    const channel = buffer[0];
    const payload = new Uint8Array(buffer.subarray(3, 3 + buffer.readUInt16BE(1)));
    buffer = buffer.subarray(3 + payload.length);
    if (channel === CHANNELS.info) infoWaiter?.(payload);
    else client.onChunk(NAMES[channel], payload);
  }
});
socket.on("close", () => client.lost());

const readInfo = () =>
  new Promise((resolve) => {
    infoWaiter = resolve;
    socket.write(record("info", new Uint8Array(0)));
  });

const info = P.decodeInfo(await readInfo());
assert.equal(info.state, "advertising");

// A wrong code: refused on our side, nothing else sent; then the right one on the same link.
await assert.rejects(client.handshake(wrongCode, info), P.WrongCodeError);
await client.handshake(code, info);

const networks = await client.scan();
const home = networks.find((n) => n.ssid === "Home-PSK");
assert.ok(home, "Home-PSK is in the scan list");

const attempt = async (psk) => {
  const statuses = [];
  for await (const s of client.provision(P.setCredentials(home.ssid, home.security, psk))) {
    statuses.push(s);
  }
  return statuses;
};
const failedTry = await attempt("not-the-password");
assert.deepEqual(failedTry.map((s) => s.state), ["connecting", "failed"]);
assert.equal(failedTry[1].reason, "wrong_password");
const success = await attempt("correct-horse-battery"); // the demo password (SPEC §10)
assert.deepEqual(success.map((s) => s.state), ["connecting", "connected"]);

console.log(JSON.stringify({ networks: networks.length, ip: success[1].ip }));
socket.end();
