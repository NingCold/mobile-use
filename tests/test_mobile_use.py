import io
from pathlib import Path

import pytest
from PIL import Image

from mobile_use.gestures import check_point, swipe_plan, tap_plan
from mobile_use.service import MobileError, MobileService


class FakeDriver:
    def __init__(self, serial):
        self.serial = serial
        self.events = []
        self.fail = False
        self.package = "example.calculator"
        self.text = "7"
        self.focused = False
        self.password = False

    @staticmethod
    def list_devices():
        return [{"serial": "test-device", "state": "device"}]

    def header(self):
        return {"package": self.package, "width": 100, "height": 200, "rotation": 0, "screen_on": True}

    def hierarchy(self):
        return f'''<hierarchy><node package="example.calculator" class="android.widget.EditText"
        text="{self.text}" bounds="[10,20][90,100]" clickable="true" enabled="true"
        focused="{str(self.focused).lower()}" password="{str(self.password).lower()}" /></hierarchy>'''

    def screenshot(self):
        b = io.BytesIO()
        Image.new("RGB", (100, 200)).save(b, format="PNG")
        return b.getvalue(), (100, 200)

    def execute(self, gesture):
        self.events.append(gesture)
        if self.fail:
            raise TimeoutError("may have executed")

    def set_text(self, text):
        self.text = text


@pytest.fixture
def service(tmp_path):
    s = MobileService(tmp_path, driver_factory=FakeDriver, validation_mode='legacy')
    s.connect("test-device")
    yield s
    s.disconnect()


def test_tap_bounded_reproducible():
    for seed in range(100):
        p = tap_plan((10, 20, 12, 22), seed)
        assert p == tap_plan((10, 20, 12, 22), seed)
        assert 10 <= p.points[0][0] < 12 and 20 <= p.points[0][1] < 22
        assert 85 <= p.hold_ms <= 140


def test_explicit_coordinate_stays_exact_for_all_seeds():
    for seed in range(100):
        assert tap_plan((50, 80, 51, 81), seed).points == [(50, 80)]


def test_fling_retains_release_velocity_and_drag_holds():
    slow = swipe_plan((50, 180), (50, 20), "browse", 1, 100, 200, 600, "steady")
    fast = swipe_plan((50, 180), (50, 20), "fling", 1, 100, 200, 600, "steady")
    drag = swipe_plan((50, 180), (50, 20), "drag", 1, 100, 200)
    assert abs(fast.points[-1][1] - fast.points[-2][1]) > abs(slow.points[-1][1] - slow.points[-2][1])
    assert all(p == (50, 180) for p in drag.points[:31])
    assert drag.as_dict()["planned_duration_ms"] == 1400
    for p in fast.points + slow.points + drag.points:
        check_point(*p, 100, 200)


@pytest.mark.parametrize("point", [(100, 0), (-1, 0), (float("nan"), 1), (1, float("inf"))])
def test_reject_outside_or_nonfinite(point):
    with pytest.raises(ValueError):
        check_point(*point, 100, 200)


def test_old_observation_cannot_replay(service):
    obs = service.observe()
    _, new = service.tap(obs.id, "e1", seed=1)
    assert new.id != obs.id
    with pytest.raises(MobileError, match="Stale"):
        service.tap(obs.id, "e1")
    assert len(service.driver.events) == 1


@pytest.mark.parametrize("change", ["package", "text"])
def test_external_changes_rejected_before_input(service, change):
    obs = service.observe()
    setattr(service.driver, change, "changed")
    with pytest.raises(MobileError, match="changed"):
        service.tap(obs.id, "e1")
    assert service.driver.events == []


def test_uncertain_input_is_not_replayed(service):
    obs = service.observe()
    service.driver.fail = True
    with pytest.raises(MobileError, match="outcome unknown"):
        service.tap(obs.id, "e1")
    assert service.observation is None
    assert len(service.driver.events) == 1


def test_dry_run_does_not_consume_or_inject(service):
    obs = service.observe()
    result, same = service.tap(obs.id, "e1", seed=9, dry_run=True)
    assert same.id == obs.id and result["dry_run"]
    assert not service.driver.events


def test_stop_requires_explicit_resume(service):
    obs = service.observe()
    service.stop()
    with pytest.raises(MobileError, match="Stopped"):
        service.tap(obs.id, "e1")
    obs = service.observe()  # ordinary observation must NOT resume
    with pytest.raises(MobileError, match="Stopped"):
        service.tap(obs.id, "e1")
    obs = service.observe(resume=True)
    service.tap(obs.id, "e1")


def test_focused_unicode_and_password_guard(service):
    obs = service.observe()
    with pytest.raises(MobileError, match="focused"):
        service.type_text(obs.id, "中文")
    service.driver.focused = True
    obs = service.observe()
    _, new = service.type_text(obs.id, "中文")
    assert new.elements[0].text == "中文"
    service.driver.password = True
    obs = service.observe()
    with pytest.raises(MobileError, match="password"):
        service.type_text(obs.id, "secret")
    assert "中文" not in (service.run_dir / "actions.jsonl").read_text(encoding="utf-8")


def test_exclusive_device_lease(service, tmp_path):
    other = MobileService(tmp_path / "other", driver_factory=FakeDriver)
    with pytest.raises(MobileError, match="owns"):
        other.connect("test-device")


def test_native_mcp_image(service):
    from mobile_use.server import pack
    result = pack({}, service.observe())
    assert result.content[1].type == "image"
    assert result.content[1].mime_type == "image/png"
    assert result.structured_content["observation"]["width"] == 100


def test_single_rpc_adapter_never_replays(monkeypatch):
    from types import SimpleNamespace
    import mobile_use.driver as module
    calls = []
    def failed(*args):
        calls.append(args)
        raise TimeoutError("server may have received request")
    monkeypatch.setattr(module, "_jsonrpc_call", failed)
    fake = SimpleNamespace(_dev=object(), _device_server_port=9008, _debug=False)
    with pytest.raises(TimeoutError):
        module.NoReplayDevice.jsonrpc_call(fake, "click", [10, 20])
    assert len(calls) == 1


def test_change_during_screenshot_invalidates_snapshot(service):
    original = service.driver.screenshot
    def changing_screenshot():
        service.driver.text += "different"
        return original()
    service.driver.screenshot = changing_screenshot
    obs = service.observe()
    assert not obs.actionable and obs.consistency == "inconsistent"
    assert obs.png and obs.capture["attempts"] == 3
    with pytest.raises(MobileError, match="inconsistent"):
        service.tap(obs.id, "e1")
    assert not service.driver.events


def test_mcp_discovery_and_error_contract(service):
    import asyncio
    from mcp import Client
    from mobile_use.server import create_server
    async def check():
        async with Client(create_server(service)) as client:
            tools = await client.list_tools()
            assert len(tools.tools) == 15
            result = await client.call_tool("observe", {})
            assert not result.is_error
            assert any(c.type == "image" for c in result.content)
            result = await client.call_tool("tap", {"observation_id": "invented", "x": 5, "y": 5})
            assert result.is_error
            assert not service.driver.events
    asyncio.run(check())


@pytest.mark.parametrize("widget", ["ProgressBar", "SeekBar", "AbsSeekBar"])
def test_live_media_progress_does_not_invalidate_static_controls(widget):
    from mobile_use.state import parse_tree
    xml = f'''<hierarchy><node package="example.video" class="android.widget.{widget}"
    text="1200.0" content-desc="Playback" bounds="[0,90][100,100]" enabled="true" />
    <node package="example.video" class="android.widget.Button" text="Search"
    clickable="true" bounds="[10,10][80,40]" /></hierarchy>'''
    elements, before = parse_tree(xml, 100, 200)
    updated, after = parse_tree(xml.replace("1200.0", "2200.0"), 100, 200)
    assert before == after
    assert elements[0].text == "1200.0" and updated[0].text == "2200.0"
    for old, new in [("Search", "Delete"), ("Playback", "Download"),
                     ("[0,90][100,100]", "[0,80][100,90]"),
                     ('enabled="true"', 'enabled="false"'), ("1200.0", "Loading")]:
        assert parse_tree(xml.replace(old, new), 100, 200)[1] != before


def test_transient_capture_race_retries_reads_not_input(service):
    original = service.driver.screenshot
    calls = []
    def screenshot():
        calls.append(1)
        if len(calls) == 1:
            service.driver.text = "new"
        return original()
    service.driver.screenshot = screenshot
    obs = service.observe()
    assert obs.elements[0].text == "new"
    assert len(calls) == 2 and not service.driver.events


def test_rejection_logs_diff_without_field_values(service):
    import json
    obs = service.observe()
    service.driver.text = "private changed text"
    with pytest.raises(MobileError) as err:
        service.tap(obs.id, "e1")
    assert err.value.details["input_status"] == "not_sent"
    log = (service.run_dir / "actions.jsonl").read_text(encoding="utf-8")
    rejected = [json.loads(line) for line in log.splitlines() if 'input_rejected' in line][-1]
    assert rejected["difference"]["changes"][0]["fields"] == ["text"]
    assert "private changed text" not in log
    assert not service.driver.events


def test_failed_post_input_observation_reports_sent(service):
    obs = service.observe()
    def fails():
        raise MobileError("Known capture failure", stage="capture_test")
    service._observe = fails
    with pytest.raises(MobileError) as err:
        service.tap(obs.id, "e1")
    assert err.value.details["input_status"] == "sent"
    assert "Known capture failure" in str(err.value)
    assert len(service.driver.events) == 1 and service.observation is None


def test_passive_banner_change_does_not_block_static_tap(service):
    original = service.driver.hierarchy
    service.driver.banner = "a"
    def hierarchy():
        return original().replace('</hierarchy>', f'<node class="android.widget.TextView" '
            f'text="{service.driver.banner}" bounds="[0,160][100,195]" /></hierarchy>')
    service.driver.hierarchy = hierarchy
    obs = service.observe()
    service.driver.banner = "b"
    service.tap(obs.id, "e1")
    assert len(service.driver.events) == 1
    assert "preflight_text_tolerated" in (service.run_dir / "actions.jsonl").read_text()


@pytest.mark.parametrize('change', ['nearby', 'clickable', 'bounds', 'new_node', 'target_text'])
def test_target_guard_never_tolerates_relevant_change(change):
    from mobile_use.state import inspect_tree, unrelated_text_only
    xml = '<hierarchy><node class="android.widget.Button" text="Safe" bounds="[10,10][90,50]" clickable="true" />' \
          '<node class="android.widget.TextView" text="A" bounds="[0,160][100,195]" /></hierarchy>'
    if change == 'nearby':
        xml = xml.replace('[0,160][100,195]', '[0,55][100,80]')
    if change == 'clickable':
        xml = xml.replace('text="A"', 'clickable="true" text="A"')
    updated = xml.replace('text="A"', 'text="B"')
    if change == 'bounds':
        updated = updated.replace('[0,160][100,195]', '[0,150][100,195]')
    if change == 'new_node':
        updated = updated.replace('</hierarchy>', '<node text="Modal" /></hierarchy>')
    if change == 'target_text':
        updated = updated.replace('text="Safe"', 'text="Delete"')
    assert not unrelated_text_only(inspect_tree(xml,100,200),inspect_tree(updated,100,200),(10,10,90,50))


@pytest.mark.parametrize('default_xml', [
    '<hierarchy><node text="Hidden" visible-to-user="false" /></hierarchy>',
    '<hierarchy><node class="android.widget.FrameLayout" bounds="[0,0][100,200]" /></hierarchy>',
])
def test_active_window_fallback_and_empty_diagnostics(default_xml):
    from types import SimpleNamespace
    from mobile_use.driver import AndroidDriver
    from mobile_use.state import inspect_tree
    calls = []
    def dump(**kwargs):
        calls.append(kwargs)
        if kwargs.get('root_in_active'):
            return '<hierarchy><node text="Visible" bounds="[0,0][100,100]" /></hierarchy>'
        return default_xml
    driver = AndroidDriver.__new__(AndroidDriver)
    driver.device = SimpleNamespace(dump_hierarchy=dump)
    tree = inspect_tree(driver.hierarchy(), 100, 200)
    assert tree.elements[0].text == 'Visible'
    assert driver.hierarchy_diagnostics['source'] == 'active_window'
    assert calls == [{}, {'root_in_active': True}]
    empty = inspect_tree('<hierarchy><node visible-to-user="false" /></hierarchy>',100,200)
    assert empty.diagnostics['hidden_nodes'] == 1 and empty.diagnostics['empty']


def test_animation_returns_bounded_regions_and_allows_distant_target(service):
    original = service.driver.hierarchy
    service.driver.frame = 0
    def hierarchy():
        service.driver.frame += 1
        x = service.driver.frame % 2
        return original().replace('</hierarchy>', f'<node class="android.view.ViewGroup" '
            f'bounds="[{x},160][{x+20},190]" /></hierarchy>')
    service.driver.hierarchy = hierarchy
    obs = service.observe()
    assert obs.unstable_regions and not obs.stable
    service.tap(obs.id, 'e1')
    assert len(service.driver.events) == 1
    obs = service.observe()
    with pytest.raises(MobileError, match='unstable'):
        service.tap(obs.id, x=10, y=175)
    assert len(service.driver.events) == 1


@pytest.mark.parametrize('attribute', ['clickable="true"', 'text="Buy"', 'resource-id="button"'])
def test_motion_never_ignores_identified_or_interactive_nodes(attribute):
    from mobile_use.state import inspect_tree, localized_motion
    xml = f'<hierarchy><node class="android.view.ViewGroup" {attribute} bounds="[0,0][20,20]" /></hierarchy>'
    assert localized_motion(inspect_tree(xml,100,200),inspect_tree(xml.replace('[0,0][20,20]','[1,1][21,21]'),100,200)) is None


def test_motion_rejects_large_layout_shift():
    from mobile_use.state import inspect_tree, localized_motion
    xml = '<hierarchy><node class="android.view.ViewGroup" bounds="[0,0][20,20]" /></hierarchy>'
    assert localized_motion(inspect_tree(xml,100,200),inspect_tree(xml.replace('[0,0][20,20]','[0,120][20,140]'),100,200)) is None


@pytest.mark.parametrize('interactive', [False, True])
def test_live_text_region_is_bounded_and_interactive_text_rejected(interactive):
    from mobile_use.state import inspect_tree, localized_text_updates, overlaps_regions
    xml = f'<hierarchy><node class="android.widget.TextView" text="15 seconds" clickable="{str(interactive).lower()}" bounds="[0,80][90,110]" /></hierarchy>'
    regions = localized_text_updates(inspect_tree(xml,100,200),inspect_tree(xml.replace('15 seconds','9 seconds').replace('[90,110]','[80,110]'),100,200))
    if interactive:
        assert regions is None
    else:
        assert regions == [(0,80,90,110)]
        assert overlaps_regions((10,85,11,86),regions)
        assert not overlaps_regions((10,190,11,191),regions)
