# Phone configurator

The configurator used for the hardware run: a web page for Chrome on Android that speaks the same encrypted protocol as `teton-configurator`, over Web Bluetooth, with WebCrypto only (SPEC §9.3, plan_v5 D3).

- `index.html`: the screens. Every text comes from `src/teton_configurator/ui_text.py` through a marked block.
- `protocol.js`: framing, the handshake, envelopes and message checks, with no DOM, so Node tests the exact code the phone runs.
- `test_protocol.mjs`, `test_client.mjs`: Node tests, run by `tests/test_web.py` when `node` is on the PATH.

How to serve it and use it: the README section "Run it for real". After changing `ui_text.py` or `protocol.py`, run `python tests/web_data.py` to refresh the copied blocks and the test vectors.
