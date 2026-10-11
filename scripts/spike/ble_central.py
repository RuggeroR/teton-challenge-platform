"""BLE spike, central side (run on laptop B with bleak). Throwaway code, not product code.

Does what the configurator will do over BLE: scan for the service UUID, connect, read the
MTU, read `info`, subscribe, write and receive notifications. With the phone instead of
laptop B, do the same by hand in nRF Connect.

Run:   .venv/bin/python scripts/spike/ble_central.py
"""

import asyncio
import sys
import time

from bleak import BleakClient, BleakScanner

SERVICE = "18388b82-f3e5-40bb-a9b9-fd0f9d789b62"
INFO = "a95e3738-82e8-4b9f-bdae-54b2ad44b843"
SESSION = "3904babf-186b-4914-b120-559051a7400d"
COMMAND = "8f3ed082-9bcf-4ea0-8794-785bf52deb1f"
STATUS = "7e81670a-c5d4-459d-8e6d-0925e5eaa9da"
NAMES = {INFO: "info", SESSION: "session", COMMAND: "command", STATUS: "status"}


def log(msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


async def main() -> int:
    log("scanning 10 s for the Teton service UUID ...")
    found = await BleakScanner.discover(timeout=10, service_uuids=[SERVICE], return_adv=True)
    if not found:
        log("no device found: is ble_peripheral.py running on laptop A?")
        return 1
    for device, adv in found.values():
        log(
            f"found {device.address} name={adv.local_name!r} rssi={adv.rssi} "
            f"uuids={adv.service_uuids}"
        )
    device, _ = next(iter(found.values()))

    results: dict[str, str] = {}
    notifications: dict[str, list[bytes]] = {SESSION: [], STATUS: []}

    def on_notify(characteristic, data: bytearray) -> None:
        uuid = str(characteristic.uuid).lower()
        notifications.setdefault(uuid, []).append(bytes(data))
        log(f"NOTIFY {NAMES.get(uuid, uuid)} <- {bytes(data)!r}")

    async with BleakClient(device) as client:
        log(f"connected to {device.address}")
        try:
            # Private bleak API: on BlueZ this is the only way to learn the negotiated MTU.
            await client._backend._acquire_mtu()
        except Exception as exc:
            log(f"could not acquire MTU: {exc!r}")
        mtu = client.mtu_size
        results["c. MTU"] = str(mtu)
        log(f"MTU = {mtu} -> chunk size {mtu - 3}")

        for char in client.services.get_service(SERVICE).characteristics:
            log(f"  {NAMES.get(char.uuid, char.uuid):8} {char.properties}")

        info = bytes(await client.read_gatt_char(INFO))
        log(f"READ info {len(info)} bytes: {info!r}")
        results["i. long read intact"] = "yes" if info.endswith(b'"END"}') else "NO"

        await client.start_notify(SESSION, on_notify)
        await client.start_notify(STATUS, on_notify)

        await client.write_gatt_char(SESSION, b"hello from bleak", response=True)
        await asyncio.sleep(2)
        results["b. write + echo notify"] = (
            "yes" if b"echo:hello from bleak" in notifications[SESSION] else "NO"
        )

        chunk = bytes(range(256))[: mtu - 3]
        await client.write_gatt_char(COMMAND, chunk, response=True)
        log(f"WRITE command {len(chunk)} bytes (one full chunk) ok")

        log("waiting 8 s for periodic status notifications ...")
        await asyncio.sleep(8)
        results["b. status notifications"] = str(len(notifications[STATUS]))

    log("disconnected")
    print("\nSummary")
    for key, value in results.items():
        print(f"  {key:26} {value}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
