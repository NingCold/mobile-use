"""Visual intent/motion regressions; ordinary target tests cover shared guards."""
import io

import pytest
from PIL import Image, ImageDraw, ImageFont

from mobile_use.service import MobileService
from mobile_use.server import invoke
from mobile_use.vision import Label, LocalVision, unique, bind_reference, relocate
from mobile_use.options import VisualTarget
from mobile_use.targets import build_graph
from mobile_use.targets import TargetError
from test_mobile_use import FakeDriver


def frame(y, text='Open', context='Report A', duplicate=False, score=.99):
    image = Image.new('RGB', (100, 200), '#eeeeee')
    draw = ImageDraw.Draw(image)
    draw.rectangle((30, y, 70, y+15), fill='#3366cc')
    draw.text((35, y), text, fill='white')
    draw.text((20, y-25), context, fill='black')
    b = io.BytesIO(); image.save(b, format='PNG')
    labels = [Label(text, score, (30, y, 71, y+16)), Label(context, .99, (20, y-25, 80, y-10))]
    if duplicate:
        labels.append(Label(text, .99, (30, 175, 71, 191)))
    return b.getvalue(), labels


@pytest.fixture
def visual(tmp_path):
    service = MobileService(tmp_path, driver_factory=FakeDriver)
    service.connect('test-device')
    service.driver.hierarchy = lambda: '<hierarchy><node package="example.calculator" class="android.view.View" bounds="[0,0][100,200]" /></hierarchy>'
    old, old_labels = frame(70)
    service.driver.screenshot = lambda: (old, (100, 200))
    oid = invoke(service.observe, image='always').structured_content['observation']['observation_id']
    class Reader:
        values = {old: old_labels}
        def read(self, png, texts=(), region=None):
            return self.values[png]
    service._vision = Reader()
    def change(y=120, **kwargs):
        png, labels = frame(y, **kwargs)
        service._vision.values[png] = labels
        service.driver.screenshot = lambda: (png, (100, 200))
        return png
    yield service, oid, change
    service.disconnect()


def tap(service, oid, **kwargs):
    return invoke(service.tap, oid, x=50, y=77, visual_bounds=[25, 65, 76, 91],
                  visual_target={'text': 'Open', 'context_text': 'Report A'}, image='never', **kwargs)


def test_local_relocation_clicks_new_target_once_without_intermediate_observation(visual, monkeypatch):
    service, oid, change = visual
    change()
    opened, original = [], Image.open
    def counted(*args, **kwargs):
        opened.append(1)
        return original(*args, **kwargs)
    monkeypatch.setattr(Image, 'open', counted)
    result = tap(service, oid)
    assert not result.is_error, result.structured_content
    data = result.structured_content
    assert data['validation']['status'] == 'relocated'
    assert data['reads']['screenshot'] == 2  # recognition frame and fresh confirmation
    assert len(service.driver.events) == 1
    assert len(opened) == 2  # Target and context share the same decoded frame pair.
    x, y = service.driver.events[0].points[0]
    assert 30 <= x < 71 and 120 <= y < 136


@pytest.mark.parametrize('change,code', [
    ({'text': 'Buy'}, 'visual_target_missing'),
    ({'context': 'Report B'}, 'visual_target_missing'),
    ({'duplicate': True}, 'visual_target_ambiguous'),
    ({'score': .8}, 'visual_low_confidence'),
])
def test_changed_or_ambiguous_intent_never_sends(visual, change, code):
    service, oid, set_frame = visual
    set_frame(**change)
    result = tap(service, oid)
    assert result.is_error and result.structured_content['code'] == code
    assert result.structured_content['input_status'] == 'not_sent'
    assert result.structured_content['recovery']['reused_validation_image']
    assert result.structured_content['reads']['screenshot'] == (2 if code == 'visual_target_missing' else 1)
    assert not service.driver.events


def test_move_during_ocr_reuses_confirmation_as_next_search_not_input_retry(visual):
    service, oid, change = visual
    first, second = change(110), change(140)
    frames = iter([first, second, second])
    service.driver.screenshot = lambda: (next(frames), (100, 200))
    result = tap(service, oid)
    assert not result.is_error, result.structured_content
    assert result.structured_content['validation']['visual']['attempts'] == 2
    assert result.structured_content['reads']['screenshot'] == 3
    assert 140 <= service.driver.events[0].points[0][1] < 156
    assert len(service.driver.events) == 1


@pytest.mark.parametrize('interruption', ['dialog', 'stop', 'foreground'])
def test_interruption_during_recognition_prevents_input(visual, interruption):
    service, oid, change = visual
    change()
    original = service._vision.read
    def read(png, texts=(), region=None):
        labels = original(png)
        if labels[0].bounds[1] == 120:
            if interruption == 'dialog':
                service.driver.hierarchy = lambda: '<hierarchy><node package="example.calculator" class="android.app.Dialog" bounds="[0,0][100,200]" /></hierarchy>'
            elif interruption == 'stop':
                service.stop()
            else:
                service.driver.package = 'other.app'
        return labels
    service._vision.read = read
    result = tap(service, oid)
    assert result.is_error and result.structured_content['input_status'] == 'not_sent'
    assert not service.driver.events


def test_unknown_visual_input_is_never_replayed(visual):
    service, oid, change = visual
    change()
    service.driver.fail = True
    result = tap(service, oid)
    assert result.is_error and result.structured_content['input_status'] == 'unknown'
    assert len(service.driver.events) == 1


def test_real_local_ocr_keeps_duplicate_candidates_and_original_coordinates():
    image = Image.new('RGB', (600, 900), 'white')
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=40)
    draw.text((80, 320), 'Report A', fill='black', font=font)
    draw.text((80, 480), 'Open', fill='black', font=font)
    draw.text((330, 690), 'Open', fill='black', font=font)
    buffer = io.BytesIO(); image.save(buffer, format='PNG')
    labels = LocalVision().read(buffer.getvalue(), texts=('Open', 'Report A'), region=[0, 250, 600, 850])
    assert unique(labels, 'Report A', (0, 250, 600, 850)).bounds[1] >= 310
    assert unique(labels, 'Open', (0, 450, 280, 570)).bounds[1] >= 470
    with pytest.raises(TargetError, match='one exact visual label'):
        unique(labels, 'Open', (0, 250, 600, 850))


def test_reflow_and_repeated_context_are_not_extra_actions(visual):
    service, oid, change = visual
    png = change()
    # Same declared item and one action; title switches sides and scales with
    # an expanded card. Its repeated logo is another witness, not a second CTA.
    service._vision.values[png] = [Label('Open', .99, (30,120,71,136)),
        Label('Report A', .99, (5,160,90,195)), Label('Report A', .99, (10,20,80,35))]
    result = tap(service, oid)
    assert not result.is_error, result.structured_content
    assert len(service.driver.events) == 1


def test_visual_card_binding_does_not_jump_to_a_different_card():
    from test_targets import node
    graph = build_graph('<hierarchy>'+node(rid='root', bounds='[0,0][100,200]', children=
        node(rid='card-a', bounds='[0,0][100,100]', children=node('A'))+
        node(rid='card-b', bounds='[0,100][100,200]', children=node('B')))+'</hierarchy>',100,200)
    spec = VisualTarget('Open','Report A')
    labels = [Label('Open',.99,(30,60,70,80)),Label('Report A',.99,(10,20,90,35))]
    binding = bind_reference(labels, spec, (25,55,75,85), (100,200), graph)
    # An unrelated second action outside the bound card does not make A ambiguous.
    target, _ = relocate(labels+[Label('Open',.99,(30,160,70,180))],spec,binding,(100,200),graph)
    assert target.bounds[1] == 60
    with pytest.raises(TargetError) as exc:
        relocate([Label('Open',.99,(30,160,70,180)),Label('Report A',.99,(10,120,90,135))],
                 spec,binding,(100,200),graph)
    assert exc.value.code == 'visual_target_missing'


def test_transient_visual_label_missing_is_reresolved_without_input_retry(visual):
    service, oid, change = visual
    absent, ready = change(text=''), change()
    frames = iter([absent, ready, ready])
    service.driver.screenshot = lambda: (next(frames),(100,200))
    result = tap(service, oid)
    assert not result.is_error, result.structured_content
    assert len(service.driver.events) == 1
    assert result.structured_content['reads']['screenshot'] == 3
