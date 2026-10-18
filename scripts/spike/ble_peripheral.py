"""BLE spike, peripheral side (run on laptop A). Throwaway code, not product code.

Answers, before we build on `bless`:
  a. Does laptop A advertise our service, without sudo?
  b. Do read, write and notify work?
  c. What MTU does BlueZ report to us?
  e. Do we notice when a client disconnects?
  f. Can we tell two clients apart?
  g. Can we stop advertising while a client stays connected?
  i. Are long reads (value longer than one packet) delivered intact?

`bless` drops the `options` BlueZ passes with every read and write (they carry `mtu`,
`device` and `offset`), so this script patches the D-Bus handlers to log them.

Run:   .venv/bin/python scripts/spike/ble_peripheral.py
Keys:  a+Enter toggle advertising, n+Enter send a status notification,
       s+Enter show state, q+Enter quit.
"""

import asyncio
import os
import sys
import time

from bless import BlessServer, GATTAttributePermissions, GATTCharacteristicProperties
from bless.backends.bluezdbus.dbus.characteristic import BlueZGattCharacteristic
from dbus_next import Message, MessageType

# Same UUIDs as SPEC §4, so the spike tests the real layout.
SERVICE = "18388b82-f3e5-40bb-a9b9-fd0f9d789b62"
INFO = "a95e3738-82e8-4b9f-bdae-54b2ad44b843"
SESSION = "3904babf-186b-4914-b120-559051a7400d"
COMMAND = "8f3ed082-9bcf-4ea0-8794-785bf52deb1f"
STATUS = "7e81670a-c5d4-459d-8e6d-0925e5eaa9da"
NAMES = {INFO: "info", SESSION: "session", COMMAND: "command", STATUS: "status"}
LOCAL_NAME = "Teton-SPIK"

# Longer than one packet at the default MTU (23 -> 22 bytes per read), to test long reads.
INFO_VALUE = (
    b'{"v":1,"device_id":"TETON-5P1KE0","name":"Teton-SPIK","state":"advertising",'
    b'"caps":["wpa2-psk","wpa3-sae","wpa2-eap-peap"],'
    b'"note":"spike value longer than one BLE packet to test read offsets","end":"END"}'
)

Props = GATTCharacteristicProperties
RW = GATTAttributePermissions.readable | GATTAttributePermissions.writeable


def log(msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


def char_name(uuid: str) -> str:
    return NAMES.get(uuid.lower(), uuid)


def patch_bless_dbus_handlers() -> None:
    """Wrap the D-Bus handlers bless registers, to log what BlueZ sends us."""

    def dbus_method(name: str):
        return getattr(BlueZGattCharacteristic, name).__dict__["__DBUS_METHOD"]

    read_m = dbus_method("ReadValue")
    original_read = read_m.fn

    def read_value(self, options):
        opts = {k: v.value for k, v in options.items()}
        log(f"READ  {char_name(self._uuid)} options={opts}")
        value = original_read(self, options)
        # bless ignores the offset and always returns the whole value. Return the
        # requested slice, as BlueZ expects, so long reads arrive intact.
        return value[opts.get("offset", 0) :]

    read_m.fn = read_value

    write_m = dbus_method("WriteValue")
    original_write = write_m.fn

    def write_value(self, value, options):
        opts = {k: v.value for k, v in options.items()}
        log(f"WRITE {char_name(self._uuid)} {len(value)} bytes options={opts}")
        return original_write(self, value, options)

    write_m.fn = write_value

    for name in ("StartNotify", "StopNotify"):
        m = dbus_method(name)
        original = m.fn

        def notify_toggle(self, _original=original, _name=name):
            log(f"{_name} {char_name(self._uuid)}")
            return _original(self)

        m.fn = notify_toggle


async def watch_connections(server: BlessServer) -> None:
    """Log BlueZ device objects appearing/disappearing and Device1.Connected changes.

    A first-time client appears as a new object (InterfacesAdded); a known one changes
    its Connected property (PropertiesChanged). BlueZ removes temporary devices ~30 s
    after they disconnect (InterfacesRemoved).
    """
    bus = server.bus
    rules = [
        "type='signal',sender='org.bluez',interface='org.freedesktop.DBus.Properties',"
        "member='PropertiesChanged',arg0='org.bluez.Device1'",
        "type='signal',sender='org.bluez',interface='org.freedesktop.DBus.ObjectManager'",
    ]
    for rule in rules:
        await bus.call(
            Message(
                destination="org.freedesktop.DBus",
                path="/org/freedesktop/DBus",
                interface="org.freedesktop.DBus",
                member="AddMatch",
                signature="s",
                body=[rule],
            )
        )

    def on_message(msg: Message) -> None:
        if msg.message_type != MessageType.SIGNAL or not msg.body:
            return
        if msg.member == "PropertiesChanged" and msg.body[0] == "org.bluez.Device1":
            changed = {k: v.value for k, v in msg.body[1].items() if k != "ManufacturerData"}
            log(f"DEVICE {msg.path} changed {changed}")
        elif msg.member == "InterfacesAdded" and "org.bluez.Device1" in msg.body[1]:
            props = msg.body[1]["org.bluez.Device1"]
            fields = {
                k: props[k].value for k in ("Address", "AddressType", "Connected") if k in props
            }
            log(f"DEVICE {msg.body[0]} appeared {fields}")
        elif msg.member == "InterfacesRemoved" and "org.bluez.Device1" in msg.body[1]:
            log(f"DEVICE {msg.body[0]} removed")

    bus.add_message_handler(on_message)


async def main() -> int:
    log(f"running as uid={os.getuid()} ({'root' if os.getuid() == 0 else 'not root'})")
    patch_bless_dbus_handlers()

    server = BlessServer(name=LOCAL_NAME)

    def on_read(characteristic):
        return characteristic.value

    def on_write(characteristic, value):
        uuid = str(characteristic.uuid).lower()
        log(f"      {char_name(uuid)} <- {bytes(value)!r}")
        characteristic.value = value
        if uuid == SESSION:
            # Echo back as a notification on the same characteristic.
            echo = server.get_characteristic(SESSION)
            echo.value = bytearray(b"echo:" + bytes(value))
            server.update_value(SERVICE, SESSION)
            log("      session -> echo notification sent")

    server.read_request_func = on_read
    server.write_request_func = on_write

    await server.add_new_service(SERVICE)
    await server.add_new_characteristic(SERVICE, INFO, Props.read, bytearray(INFO_VALUE), RW)
    await server.add_new_characteristic(SERVICE, SESSION, Props.write | Props.notify, None, RW)
    await server.add_new_characteristic(SERVICE, COMMAND, Props.write, None, RW)
    await server.add_new_characteristic(SERVICE, STATUS, Props.notify, None, RW)

    try:
        await server.start()
    except Exception as exc:
        log(f"FAILED to register or advertise: {exc!r}")
        log("If this is a permission error, retry with: sudo .venv/bin/python <this script>")
        return 1

    await watch_connections(server)
    log(f"advertising as {LOCAL_NAME}, service {SERVICE}, info value {len(INFO_VALUE)} bytes")
    log("keys: a=toggle advertising  n=notify status  s=state  q=quit  (then Enter)")

    advertising = True
    counter = 0
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()

    def notify_status() -> None:
        nonlocal counter
        counter += 1
        server.get_characteristic(STATUS).value = bytearray(f"status #{counter}".encode())
        server.update_value(SERVICE, STATUS)
        log(f"      status -> notification #{counter}")

    async def toggle_advertising() -> None:
        nonlocal advertising
        if advertising:
            await server.app.stop_advertising(server.adapter)
        else:
            await server.app.start_advertising(server.adapter)
        advertising = not advertising
        log(f"advertising={advertising} (adapter reports {await server.is_advertising()})")

    def on_key() -> None:
        key = sys.stdin.readline().strip().lower()
        if key == "a":
            loop.create_task(toggle_advertising())
        elif key == "n":
            notify_status()
        elif key == "s":
            log(f"advertising={advertising} subscribed={server.app.subscribed_characteristics}")
        elif key == "q":
            stop.set()

    loop.add_reader(sys.stdin.fileno(), on_key)

    async def periodic_status() -> None:
        while True:
            await asyncio.sleep(3)
            if STATUS in server.app.subscribed_characteristics:
                notify_status()

    ticker = loop.create_task(periodic_status())
    try:
        await stop.wait()
    finally:
        ticker.cancel()
        loop.remove_reader(sys.stdin.fileno())
        if not advertising:
            # bless.stop() expects an active advertisement to remove.
            await server.app.start_advertising(server.adapter)
        await server.stop()
        log("stopped (adapter alias reset to the system default)")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        pass
