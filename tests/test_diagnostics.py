"""Lazy artifacts and useful local errors without leaking input or replaying it."""
import json

from mobile_use.diagnostics import exception_summary
from mobile_use.service import MobileService
from mobile_use.server import invoke
from test_mobile_use import FakeDriver, service


def test_run_directory_is_created_only_when_an_artifact_is_written(tmp_path):
    root = tmp_path / 'runs'
    s = MobileService(root, driver_factory=FakeDriver)
    path = s.run_dir
    assert s.get_status()['trace_dir'] == str(path) and not root.exists()
    s.disconnect()
    assert not root.exists()
    s.connect('test-device')
    try:
        assert s.run_dir == path and (path / 'actions.jsonl').is_file()
    finally:
        s.disconnect()
    events = [json.loads(line)['event'] for line in (path/'actions.jsonl').read_text().splitlines()]
    assert events == ['connect', 'disconnect']


def test_first_saved_screenshot_uses_the_same_lazy_directory(tmp_path):
    s = MobileService(tmp_path / 'runs', driver_factory=FakeDriver, save_screenshots=True)
    s.driver = FakeDriver('offline-test')
    assert not s.run_dir.exists()
    obs = s.observe()
    assert (s.run_dir / f'{obs.id}.png').read_bytes() == obs.png
    assert (s.run_dir / 'actions.jsonl').is_file()
    s.disconnect()


def test_exception_summary_keeps_reason_and_bounds_redacted_details():
    reason = 'Android did not acknowledge the key'
    assert exception_summary(RuntimeError(reason), 'input.back') == {
        'type': 'RuntimeError', 'stage': 'input.back', 'message': reason}
    explicit = exception_summary(RuntimeError('Failed value private-input'), 'input.tap', ['private-input'])
    assert 'private-input' not in explicit['message']
    for message in ['Failed with "private-input"',
                    'RPC params=private-input', 'HTTP https://user:pass@example.test?q=private-input',
                    'HTTP Bearer private-input', '<node text="private-input"/>',
                    'Transport failed\nprivate-input']:
        result = exception_summary(RuntimeError(message), 'input.tap')
        assert 'private-input' not in json.dumps(result)
    long = exception_summary(OSError(5, 'x'*1000), 'read.header')
    assert long['errno'] == 5 and long['truncated'] and len(long['message']) == 512


def test_text_input_failure_logs_redacted_cause_without_replay(service):
    service.driver.focused = True
    service.driver.text = 'old-private-value'
    oid = invoke(service.observe, image='never').structured_content['observation']['observation_id']
    calls = []
    def fail(value):
        calls.append(value)
        raise RuntimeError(f'Could not replace old-private-value with {value}')
    service.driver.set_text = fail
    result = invoke(service.type_text, oid, 'new-private-value', mode='append', image='never')
    assert result.is_error and result.structured_content['input_status'] == 'unknown'
    assert calls == ['old-private-valuenew-private-value']
    log = (service.run_dir/'actions.jsonl').read_text(encoding='utf-8')
    assert 'old-private-value' not in log and 'new-private-value' not in log
    entry = next(json.loads(line) for line in log.splitlines() if json.loads(line)['event'] == 'action_unknown')
    assert entry['exception']['stage'] == 'input.type_text'
    assert entry['exception']['message'].startswith('Could not replace ')
    assert 'exception' not in result.structured_content  # Detailed cause stays in local audit.


def test_device_read_failure_preserves_stage_and_errno(service):
    def fail():
        raise OSError(10061, 'Connection refused')
    service.driver.header = fail
    result = invoke(service.observe, image='never')
    assert result.is_error and not service.driver.events
    entries = [json.loads(line) for line in (service.run_dir/'actions.jsonl').read_text().splitlines()]
    error = next(e['exception'] for e in entries if e['event'] == 'device_read_failed')
    assert error['stage'] == 'read.header' and error['errno'] == 10061
    assert 'Connection refused' in error['message']
