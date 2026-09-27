"""ANDC-T7 end to end: a screenshot is transient (docs/23 §6 level 4).

Through the real runtime, engine, tool registry, Android adapter and device
hub, to a fake device that answers `capture_screenshot` with an image
carrying a unique marker: the image reaches the configured vision model and
nothing else. It is not in the database (any table, any column, the file's
raw bytes), not in a log line, not in anything the worker model saw, and not
in the task's response — only the model's description is.
"""

from __future__ import annotations

import base64
import logging
import uuid

from sqlalchemy import select

from server.execution.device_hub import DeviceHub
from server.models.provider import ModelPricing, ModelResult, ModelSpec
from server.storage.models import UsageEvent
from server.tools.device_vision import ScreenshotVision
from server.tools.platforms import android_device_read_tool
from shared.schemas.enums import UsageKind
from tests.fake_device import DeviceLocalState, FakeDevice
from tests.runtime.conftest import ask, call, final

PACKAGE = "com.example"
SCOPE = {"package_name": PACKAGE}


class VisionModel:
    def __init__(self) -> None:
        self.spec = ModelSpec(provider="ollama", model="vision-local", pricing=ModelPricing())
        self.images: list[bytes] = []

    async def invoke(self, messages, *, timeout: float) -> ModelResult:
        self.images.extend(image.data for message in messages for image in message.images)
        return ModelResult(content="A shopping list with three items and a Save button.",
                           prompt_tokens=900, completion_tokens=40)

    async def health(self) -> bool:
        return True


async def test_a_screenshot_reaches_only_the_vision_model(make_harness, tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    marker = f"SCREEN-PIXELS-{uuid.uuid4().hex}".encode()
    image = b"RIFF\x20\x00\x00\x00WEBPVP8 " + marker
    image_b64 = base64.b64encode(image).decode()

    hub = DeviceHub()
    vision = VisionModel()
    tool = android_device_read_tool(hub, vision=ScreenshotVision(vision))
    h = await make_harness(extra_tools=[tool])
    alice = await h.user("alice")
    device = await FakeDevice(
        device_id=alice.device_id,
        state=DeviceLocalState(packages={PACKAGE: {"screenshot"}}, app_policy={"non_sensitive": [PACKAGE]}),
        results={"accessibility.screenshot": (
            {"app": {"package_name": PACKAGE}, "image_webp_base64": image_b64, "width": 2, "height": 2}, None)},
    ).attach(hub, user_id=alice.user_id)
    await h.grant(alice, "device.read", resource_scope=SCOPE)

    h.model.push(ask("device.read", scope=SCOPE),
                 call("device.read", "capture_screenshot", platform="android"),
                 final("Your screen shows a shopping list."))
    resp = await h.submit(alice, "what's on my screen?")
    assert resp.status_code == 200, resp.text

    # It happened: the device captured, the vision model saw the image once,
    # and the worker read the description.
    assert device.executed == ["accessibility.screenshot"]
    assert vision.images == [image]
    assert "A shopping list with three items" in h.model.all_text()

    # …and the image went nowhere else.
    for text in (h.model.all_text(), resp.text, caplog.text):
        assert image_b64 not in text and marker.decode() not in text
    for db in tmp_path.glob("*.db*"):
        raw = db.read_bytes()
        assert marker not in raw and image_b64.encode() not in raw, db.name

    # The vision call was metered as a model call against the device.read tool.
    async with h.storage.session() as s:
        usage = (await s.execute(select(UsageEvent).where(UsageEvent.user_id == alice.user_id,
                                                          UsageEvent.tool_id == "device.read"))).scalars().all()
    assert [(u.kind, u.model) for u in usage] == [(UsageKind.MODEL_CALL, "vision-local")]
