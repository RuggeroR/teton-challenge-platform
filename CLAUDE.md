# CLAUDE.md

Guidance for Claude Code sessions working in this repository.

## What this is

A take-home challenge for Teton.ai (Software Engineer, Fleet). It is a **Wi-Fi provisioning flow over Bluetooth Low Energy**: a configurator (laptop B) sends Wi-Fi credentials, encrypted end to end, to a "Teton Device" (laptop A, Ubuntu). The device joins the network through NetworkManager, verifies internet access and reports the result back.

## Read before changing anything

| File | Role |
|---|---|
| `docs/plan_v5.md` | **Source of truth for decisions and their rationale.** The owner must be able to defend every decision in an interview. (`plan_v4.md` is kept for history.) |
| `docs/IMPLEMENTATION_PLAN.md` | Build order, step-by-step plan and open review questions |
| `docs/SPEC.md` | What to build: protocol, messages, crypto parameters, CLI behaviour, acceptance criteria |
| `docs/DESIGN.md` | How to build it: module map, interfaces, state machine, testing strategy |
| `docs/research/channels.md` | Why BLE was chosen over the other channels (input for the writeup) |

Rules:
- **Do not change a decision silently.** If implementation shows a decision in the latest `plan_vN.md` is wrong or impossible, stop and explain the problem to the owner. Once agreed, record it in the plan (a new `plan_vN+1.md` for a new review round, or a new changelog row in the current one) and update SPEC/DESIGN to match.
- Work in the scope order from SPEC §12: **MUST**, then SHOULD, then COULD. Don't build COULD items unless asked.
- The owner wants to understand every part. Explain non-obvious choices in the PR description or in a short comment, and keep the code simple over clever.
- Do not commit changes by yourself. Explain when something is ready to be commited.

## Environment

- Target: **Ubuntu 24.04 LTS**, Python **3.12**, BlueZ 5.72, NetworkManager 1.46. Don't add support for older versions.
- Setup: `python3 -m venv .venv && . .venv/bin/activate && pip install -e ".[dev]"`
- Tests: `pytest`
- Lint/format: `ruff check . && ruff format --check .` (fix with `ruff format .`)
- Real BLE and Wi-Fi only work on the two laptops. Everything else must run without hardware, using the in-memory transport and the fake Wi-Fi backend.

## Architecture in one screen

- `src/teton_common/`: shared and pure (no I/O). Protocol constants, JSON messages, framing, crypto, setup code, transport interface, logging.
- `src/teton_device/`: `teton-device` CLI. `service.py` orchestrates; `session.py` speaks the protocol; `state.py` is the state machine; `wifi/` wraps `nmcli`; `gatt_server.py` (BLE) and `sim_server.py` (Unix socket) are transports.
- `src/teton_configurator/`: `teton-configurator` CLI. `flow.py` is the guided UX; `client.py` speaks the protocol; `ble_client.py` and `sim_client.py` are transports.
- One protocol implementation, many transports. Side effects (radio, Wi-Fi, time, randomness) sit behind interfaces so tests can inject fakes.

## Non-negotiables

- **Security:** follow SPEC §5 exactly (X25519 + HKDF salted with the setup code, HMAC key confirmation, AES-256-GCM with per-direction sequence numbers). Use only the `cryptography` library and never hand-roll primitives. Compare tags in constant time (`hmac.compare_digest`).
- **No secrets in logs, errors or exceptions:** passwords, PSKs, the setup code, keys and ciphertext. Logging goes through the redaction processor in `teton_common/log.py`. Keep the test that checks this.
- **No plaintext credentials over BLE.** Only `info` and the handshake are plaintext, and neither contains secrets.
- **Wi-Fi changes only through the `WifiBackend` interface.** Never call `nmcli` from elsewhere.
- **Plain language for the technician:** every user-facing failure is one sentence plus a next action, defined in `teton_configurator/ui_text.py`.

## Conventions

- Python 3.12, type hints everywhere, `asyncio` for I/O, dataclasses for messages and results.
- Small modules that match the layout in DESIGN §2. Don't add new top-level packages without a reason.
- Tests sit in `tests/`, named after the module (`test_crypto.py`, …). Write the test with the feature, not later.
- Dependencies are limited to those in DESIGN §8. Ask before adding one.
- Commits are small and descriptive. Use one branch per milestone and open a PR into `main`.

## Status

The repository currently holds the documentation and an **empty skeleton**: every source file contains only a comment describing its purpose. Implementation starts with the MUST items: `teton_common` (setup code, crypto, framing, messages) with tests, then the device Wi-Fi backend, then the BLE transports and CLIs.
