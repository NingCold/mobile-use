from mobile_use.state import inspect_tree, localized_subtree_updates, overlaps_regions


def tree(card="", target="Go", overlay=""):
    return inspect_tree(f'''<hierarchy>
      <node package="app" class="android.widget.FrameLayout" bounds="[0,0][1080,2400]"/>
      {card}
      <node package="app" class="android.widget.Button" text="{target}" clickable="true" enabled="true" bounds="[800,2200][1050,2340]"/>
      {overlay}</hierarchy>''', 1080, 2400)


def test_banner_insertion_does_not_shift_target_into_diff():
    card = '<node package="app" class="android.widget.TextView" text="new" bounds="[80,300][700,380]"/>'
    regions = localized_subtree_updates(tree(), tree(card))
    assert regions == [(80, 300, 700, 380)]
    assert not overlaps_regions((800, 2200, 1050, 2340), regions)


def test_target_change_remains_inside_blocked_region():
    regions = localized_subtree_updates(tree(), tree(target="Pay"))
    assert regions and overlaps_regions((800, 2200, 1050, 2340), regions)


def test_large_dialog_and_distant_combined_changes_are_rejected():
    dialog = '<node package="app" class="android.widget.FrameLayout" bounds="[50,500][1030,1800]"/>'
    assert localized_subtree_updates(tree(), tree(overlay=dialog)) is None
    card = '<node package="app" class="android.widget.TextView" text="new" bounds="[80,300][700,380]"/>'
    assert localized_subtree_updates(tree(), tree(card, target="Pay")) is None


def test_capture_insertion_marks_motion_and_blocks_nearby_input(tmp_path):
    import io
    import pytest
    from PIL import Image
    from mobile_use.service import MobileService, MobileError

    class Driver:
        def __init__(self, serial):
            self.serial, self.inserted, self.events = serial, False, []
        @staticmethod
        def list_devices():
            return [{"serial": "animation-test", "state": "device"}]
        def header(self):
            return {"package": "app", "width": 1080, "height": 2400,
                    "rotation": 0, "screen_on": True}
        def hierarchy(self):
            card = ('<node package="app" class="android.view.ViewGroup" '
                    'bounds="[800,1950][1000,2150]"/>') if self.inserted else ''
            return ('<hierarchy><node package="app" class="android.widget.FrameLayout" '
                    'bounds="[0,0][1080,2400]"/>' + card + '</hierarchy>')
        def screenshot(self):
            self.inserted = True
            out = io.BytesIO()
            Image.new('RGB', (1080, 2400)).save(out, format='PNG')
            return out.getvalue(), (1080, 2400)
        def execute(self, gesture):
            self.events.append(gesture)

    service = MobileService(tmp_path, driver_factory=Driver, validation_mode='legacy')
    service.connect('animation-test')
    try:
        obs = service.observe()
        assert obs.unstable_regions == [(800, 1950, 1000, 2150)]
        assert not obs.stable
        with pytest.raises(MobileError):
            service.tap(obs.id, x=900, y=2030)
        assert not service.driver.events
    finally:
        service.disconnect()
