# Tests for the phone page (web/, plan_v5 D3): what it copies from the Python code is
# current (review Q105, Q108), its protocol code matches the Python vectors byte for byte,
# and its Client completes a whole provisioning run against the real DeviceService over the
# simulation socket. The two Node tests are skipped where `node` isn't installed; the page's
# screens and the real radio are checked by hand (Step 6.2b Verify).

import asyncio
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import web_data
from fake_configurator import CODE, WRONG_CODE
from test_sim_transport import SimDevice

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
NODE_WAIT_S = 30  # real seconds; the runs take well under one


def test_constants_in_protocol_js_are_current() -> None:
    block = web_data.protocol_js_block()
    assert block in web_data.PROTOCOL_JS.read_text(), "run: python tests/web_data.py"


def test_texts_in_index_html_are_current() -> None:
    block = web_data.index_html_block()
    assert block in web_data.INDEX_HTML.read_text(), "run: python tests/web_data.py"


def test_vectors_are_current() -> None:
    assert web_data.VECTORS.read_text() == web_data.vectors_json(), "run: python tests/web_data.py"


def test_every_text_the_page_uses_is_copied() -> None:
    """The page only reads T.<NAME>: each must be in the generated block (from ui_text.py)."""
    page = web_data.INDEX_HTML.read_text()
    script = page.split(web_data.END, 1)[1]  # the code after the generated block
    used = set(re.findall(r"\bT\.([A-Z_]+)", script))
    assert used <= set(web_data.texts()), used - set(web_data.texts())


def test_the_page_has_no_text_of_its_own() -> None:
    """Texts come from ui_text.py only: no quoted sentence in the page's code (Q105)."""
    script = web_data.INDEX_HTML.read_text().split(web_data.END, 1)[1]
    sentences = re.findall(r'"([A-Z][a-z]+ [^"]*\.)"', script)
    assert sentences == []


@needs_node
def test_protocol_js_matches_the_python_vectors() -> None:
    result = _node("test_protocol.mjs", str(web_data.VECTORS))
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip().endswith("all passed")


@needs_node
async def test_the_page_client_provisions_the_real_device(tmp_path: Path) -> None:
    """protocol.js's Client against DeviceService over the socket: a wrong code, the right
    code, a scan, a wrong password, then the right one (the fake Wi-Fi's Home-PSK)."""
    socket = tmp_path / "teton-sim.sock"
    device = SimDevice(socket, tmp_path / "state")
    try:
        await device.pair()
        node = await asyncio.create_subprocess_exec(
            "node",
            "--no-warnings",
            str(web_data.ROOT / "web" / "test_client.mjs"),
            str(socket),
            CODE,
            WRONG_CODE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(node.communicate(), NODE_WAIT_S)
        assert node.returncode == 0, out.decode() + err.decode()
        assert json.loads(out.decode().splitlines()[-1]) == {"networks": 4, "ip": "192.168.1.23"}
        assert device.service.state() == "provisioned"
    finally:
        await device.power_off()


def _node(script: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["node", "--no-warnings", str(web_data.ROOT / "web" / script), *args],
        capture_output=True,
        text=True,
        timeout=NODE_WAIT_S,
        cwd=web_data.ROOT,
    )
