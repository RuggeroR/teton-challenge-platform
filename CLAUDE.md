# CLAUDE.md

Guidance for Claude Code sessions working in this repository.

## What this is

A take-home challenge for Teton.ai (Software Engineer, Fleet). It is a **Wi-Fi provisioning flow over Bluetooth Low Energy**: a configurator (laptop B) sends Wi-Fi credentials, encrypted end to end, to a "Teton Device" (laptop A, Ubuntu). The device joins the network through NetworkManager, verifies internet access and reports the result back. The device has no text input, only simulated buttons and a status "LED"; it advertises only during a pairing window opened by its [P] button.

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

- Device (laptop A): **Ubuntu 24.04 LTS**, Python **3.12**, BlueZ 5.72, NetworkManager 1.46. This is the reference platform.
- Configurator (laptop B): Ubuntu 24.04 or 26.04 LTS. Laptop 2 runs 26.04 with Python 3.14.
- `requires-python = ">=3.12"`. **Use no language or standard-library feature newer than 3.12**, so the same code runs on both laptops. Don't add support for older versions.
- Setup: `python3 -m venv .venv && . .venv/bin/activate && pip install -e ".[dev]"`
- Tests: `pytest` (CI, a SHOULD, runs them on Python 3.12 and 3.14)
- Lint/format: `ruff check . && ruff format --check .` (fix with `ruff format .`)
- Real BLE and Wi-Fi only work on the two laptops. Everything else must run without hardware, using the in-memory transport and the fake Wi-Fi backend.

## Architecture in one screen

- `src/teton_common/`: shared and pure (no I/O). Protocol constants, JSON messages, framing, crypto, setup code, transport interface, logging.
- `src/teton_device/`: `teton-device run [--simulate] [--state-dir] [--verbose]` (no other subcommands). `service.py` orchestrates; `session.py` speaks the protocol; `state.py` is the state machine; `buttons.py` reads single keys as buttons ([P] pairing, [R][R] reset, [S] status, [Q] power off) and draws the status line; `wifi/` wraps `nmcli`; `gatt_server.py` (BLE) and `sim_server.py` (Unix socket) are transports.
- `src/teton_configurator/`: `teton-configurator` CLI. `flow.py` is the guided UX; `client.py` speaks the protocol; `ble_client.py` and `sim_client.py` are transports.
- One protocol implementation, many transports. Side effects (radio, Wi-Fi, buttons, time, randomness) sit behind interfaces so tests can inject fakes.

## Non-negotiables

- **Security:** follow SPEC §5 exactly (X25519 + HKDF salted with the setup code, HMAC key confirmation, AES-256-GCM with per-direction sequence numbers). Use only the `cryptography` library and never hand-roll primitives. Compare tags in constant time (`hmac.compare_digest`).
- **No secrets in logs, errors or exceptions:** passwords, PSKs, the setup code, keys and ciphertext. Logging goes through the redaction processor in `teton_common/log.py`. Keep the test that checks this.
- **No plaintext credentials over BLE.** Only `info` and the handshake are plaintext, and neither contains secrets.
- **No text input on the device.** It never reads typed text (no passwords, codes or commands at runtime). Its only inputs are the single-key buttons in `buttons.py`. Everything typed happens on the configurator.
- **Advertise only during the pairing window** opened by [P] (5 minutes), never automatically on start.
- **Wi-Fi changes only through the `WifiBackend` interface.** Never call `nmcli` from elsewhere.
- **Plain language for the technician:** every user-facing failure is one sentence plus a next action, defined in `teton_configurator/ui_text.py`.

## Conventions

- Python 3.12 language level (it must also run on 3.14), type hints everywhere, `asyncio` for I/O, dataclasses for messages and results.
- Small modules that match the layout in DESIGN §2. Don't add new top-level packages without a reason.
- Tests sit in `tests/`, named after the module (`test_crypto.py`, …). Write the test with the feature, not later.
- Dependencies are limited to those in DESIGN §8. Ask before adding one.
- Commits are small and descriptive. Use one branch per milestone and open a PR into `main`.

## Status

Phases 1, 2 and 3 of `docs/IMPLEMENTATION_PLAN.md` are done, and most of Phase 0 (628 tests pass):

- **Phase 0:** tooling and the BLE spike are done; the spike results are in the plan. Step 0.3, the laptop B install check on Python 3.14, has no results recorded yet.
- **Phase 1:** `teton_common` is complete: protocol, setup code, messages, framing, crypto, log, transport and the memory transport.
- **Phase 2:** the device core is complete: `state.py`, `storage.py`, `qr.py`, the Wi-Fi backend (`wifi/*`, real `nmcli` checks of Step 2.5 run on laptop 1), `session.py`, `buttons.py` and `service.py`.
- **Phase 3:** the configurator core is complete: `ui_text.py`, `client.py` (`ClientChannel` + `ProvisioningClient`) and `flow.py` (`run_flow` with injected `discover`/`connect`/`io`).

Every review question up to Q47 is decided and recorded in `plan_v5.md`. Only Q16 (CI earlier) is open.

**Next is Phase 4:** the in-memory end-to-end test and the "no secrets in logs" test (`tests/test_end_to_end.py`, `tests/test_no_secrets_in_logs.py`, `tests/conftest.py`; still comment-only). `gatt_server.py`, `sim_server.py`, `ble_client.py` and `sim_client.py` are still comment-only skeletons, and both `cli.py` files are placeholders that only print "not implemented yet" (Step 5.2).

`tests/fake_configurator.py` holds the shared test helpers: a low-level `Configurator` built on the real `ClientChannel`, the `Device` rig (a running `DeviceService` with the memory transport, fake Wi-Fi, `QueueButtons` and `FakeTime`) and `FakeTime`.
