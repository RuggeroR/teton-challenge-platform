// Checks web/protocol.js against the vectors the Python code made (review Q108):
// setup codes, framing, the handshake with fixed keys, envelopes both ways, and the JSON
// bytes. Any difference means the phone and the device would not understand each other.
//
// Run: node --no-warnings web/test_protocol.mjs tests/fixtures/web_vectors.json
// (tests/test_web.py runs it; regenerate the vectors with `python tests/web_data.py`.)

import assert from "node:assert/strict";
import { webcrypto } from "node:crypto";
import { readFileSync } from "node:fs";

globalThis.crypto ??= webcrypto; // Node 18 has WebCrypto, but not as a global yet
const P = await import("./protocol.js");
const V = JSON.parse(readFileSync(process.argv[2], "utf8"));

const hex = (h) => Uint8Array.from(h.match(/../g) ?? [], (b) => parseInt(b, 16));
const toHex = (b) => Buffer.from(b).toString("hex");
const text = (b) => new TextDecoder().decode(b);

let failed = 0;
async function test(name, fn) {
  try {
    await fn();
    console.log(`ok    ${name}`);
  } catch (e) {
    failed += 1;
    console.log(`FAIL  ${name}\n${e.stack}`);
  }
}

/** The same randomness the Python vectors used: private key, then nonce. */
const fixedRandom = () => {
  const values = [hex(V.handshake.configurator_private_hex), hex(V.handshake.configurator_nonce_hex)];
  return (n) => {
    const v = values.shift();
    assert.equal(v.length, n);
    return v;
  };
};

async function keys() {
  const handshake = await P.Handshake.start(V.handshake.typed_code, fixedRandom());
  return (await handshake.onHelloReply(P.decodeSession(P.utf8(V.handshake.hello_reply)))).keys;
}

// --- setup codes (review Q111) ------------------------------------------------------------

await test("setup codes typed in different ways normalise as in Python", () => {
  for (const { typed, code } of V.setup_codes.valid) assert.equal(P.normalizeCode(typed), code);
});

await test("invalid setup codes are refused before anything is sent", () => {
  for (const typed of V.setup_codes.invalid) {
    assert.throws(() => P.normalizeCode(typed), P.InvalidSetupCode, JSON.stringify(typed));
  }
});

// --- framing ------------------------------------------------------------------------------

await test("frame() cuts the same chunks as Python", () => {
  assert.deepEqual(P.frame(hex(V.framing.payload_hex)).map(toHex), V.framing.chunks_hex);
  assert.deepEqual(P.frame(new Uint8Array(0)).map(toHex), V.framing.empty_chunks_hex);
});

await test("the reassembler returns the payload once, after the last chunk", () => {
  const r = new P.Reassembler();
  const results = V.framing.chunks_hex.map((c) => r.feed(hex(c)));
  assert.ok(results.slice(0, -1).every((x) => x === null));
  assert.equal(toHex(results.at(-1)), V.framing.payload_hex);
});

await test("a 10 s gap drops a partial message; steady progress does not (Q94)", () => {
  let now = 0;
  const r = new P.Reassembler(() => now);
  const chunks = V.framing.chunks_hex.map(hex);
  for (const c of chunks.slice(0, -1)) {
    assert.equal(r.feed(c), null);
    now += 9; // longer than 10 s in total, but never 10 s between two chunks
  }
  assert.equal(toHex(r.feed(chunks.at(-1))), V.framing.payload_hex);
  r.feed(chunks[0]);
  now += 10.1;
  assert.deepEqual(r.feed(Uint8Array.of(0, 1)), null); // a new message starts afresh
  assert.equal(text(r.feed(P.utf8("x"))), "x");
});

await test("bad framing is refused", () => {
  const r = new P.Reassembler();
  assert.throws(() => r.feed(new Uint8Array(0)), P.FramingError);
  assert.throws(() => r.feed(Uint8Array.of(0xff, 0xff)), P.FramingError); // too large
  assert.throws(() => r.feed(Uint8Array.of(0, 1, 65, 66)), P.FramingError); // data after the end
});

// --- the handshake --------------------------------------------------------------------------

await test("hello is byte for byte the Python one", async () => {
  const handshake = await P.Handshake.start(V.handshake.typed_code, fixedRandom());
  assert.equal(text(P.encode(handshake.hello())), V.handshake.hello);
});

await test("the device's reply checks out, and confirm and the keys match Python", async () => {
  const handshake = await P.Handshake.start(V.handshake.typed_code, fixedRandom());
  const reply = P.decodeSession(P.utf8(V.handshake.hello_reply));
  assert.equal(reply.deviceId, V.handshake.device_id);
  const { confirm, keys } = await handshake.onHelloReply(reply);
  assert.equal(text(P.encode(confirm)), V.handshake.confirm);
  assert.equal(toHex(keys.c2d), V.handshake.c2d_key_hex);
  assert.equal(toHex(keys.d2c), V.handshake.d2c_key_hex);
});

await test("a wrong setup code is rejected, and so is a tampered tag", async () => {
  const wrong = await P.Handshake.start(V.handshake.wrong_code, fixedRandom());
  const reply = P.decodeSession(P.utf8(V.handshake.hello_reply));
  await assert.rejects(wrong.onHelloReply(reply), P.WrongCodeError);
  const tampered = { ...reply, confirm: reply.confirm.map((b, i) => (i === 0 ? b ^ 1 : b)) };
  const right = await P.Handshake.start(V.handshake.typed_code, fixedRandom());
  await assert.rejects(right.onHelloReply(tampered), P.WrongCodeError);
});

await test("a handshake object is single use", async () => {
  const handshake = await P.Handshake.start(V.handshake.typed_code, fixedRandom());
  const reply = P.decodeSession(P.utf8(V.handshake.hello_reply));
  await handshake.onHelloReply(reply);
  await assert.rejects(handshake.onHelloReply(reply), P.ProtocolError);
});

// --- envelopes -----------------------------------------------------------------------------

await test("the page's commands are byte for byte the Python ones", () => {
  assert.equal(text(P.encode(P.scan())), V.c2d[0].plaintext);
  const creds = P.setCredentials("Café-Home", "wpa2-psk", "correct-horse-battery");
  assert.equal(text(P.encode(creds)), V.c2d[1].plaintext);
});

await test("sealed commands match Python's envelopes, seq counting up", async () => {
  const cipher = await P.Cipher.create(await keys());
  for (const { plaintext, envelope } of V.c2d) {
    assert.equal(text(P.encode(await cipher.seal(P.utf8(plaintext)))), envelope);
  }
});

await test("the device's envelopes open and decode", async () => {
  const cipher = await P.Cipher.create(await keys());
  const decoded = [];
  for (const { plaintext, envelope } of V.d2c) {
    const opened = await cipher.open(P.decodeEnvelope(P.utf8(envelope)));
    assert.equal(text(opened), plaintext);
    decoded.push(P.decodeStatus(opened));
  }
  assert.deepEqual(decoded.map((m) => m.type), ["ready", "scan_result", "status", "status",
    "status", "error"]);
  assert.equal(decoded[1].networks[0].ssid, "Café-Home");
  assert.equal(decoded[3].reason, "wrong_password");
  assert.equal(decoded[4].ip, "192.168.43.17");
  assert.deepEqual(decoded[4].warnings, ["limited_internet"]);
  assert.equal(decoded[5].code, "unsupported_security");
});

await test("replayed, skipped and tampered envelopes are rejected", async () => {
  const [first, second] = V.d2c.map(({ envelope }) => P.decodeEnvelope(P.utf8(envelope)));
  const replay = await P.Cipher.create(await keys());
  await replay.open(first);
  await assert.rejects(replay.open(first), P.ProtocolError);
  const skip = await P.Cipher.create(await keys());
  await assert.rejects(skip.open(second), P.ProtocolError);
  const tamper = await P.Cipher.create(await keys());
  const ct = first.ct.slice();
  ct[0] ^= 1;
  await assert.rejects(tamper.open({ seq: 0, ct }), P.ProtocolError);
});

// --- messages ------------------------------------------------------------------------------

await test("info decodes", () => {
  const info = P.decodeInfo(P.utf8(V.info));
  assert.equal(info.deviceId, V.handshake.device_id);
  assert.equal(info.state, "advertising");
});

await test("messages the page doesn't expect are refused", () => {
  const bad = [
    '{"type":"hello","v":1,"pub":"","nonce":""}', // the configurator never receives hello
    '{"type":"handshake_failed","extra":1}',
    '{"type":"error","code":"rate_limited"}', // without retry_after_s
    "not json",
  ];
  for (const m of bad) assert.throws(() => P.decodeSession(P.utf8(m)), P.ProtocolError, m);
  assert.throws(() => P.decodeStatus(P.utf8('{"type":"status","state":"connected"}')),
    P.ProtocolError);
  assert.throws(() => P.decodeEnvelope(P.utf8('{"seq":0,"ct":"not base64!"}')), P.ProtocolError);
});

// --- the client's waits --------------------------------------------------------------------

await test("no reply is NoReplyError, a dropped link is ConnectionLostError", async () => {
  const quiet = new P.Client(async () => {});
  await assert.rejects(quiet.receive(0.05), P.NoReplyError);
  const dropping = new P.Client(async () => {});
  setTimeout(() => dropping.lost(), 10);
  await assert.rejects(dropping.receive(5), P.ConnectionLostError);
  const failing = new P.Client(async () => {
    throw new Error("GATT write failed");
  });
  const info = P.decodeInfo(P.utf8(V.info));
  await assert.rejects(failing.handshake(V.handshake.typed_code, info), P.ConnectionLostError);
  await assert.rejects(new P.Client(async () => {}).scan(), P.ProtocolError); // no session yet
});

if (failed) {
  console.log(`${failed} failed`);
  process.exit(1);
}
console.log("all passed");
