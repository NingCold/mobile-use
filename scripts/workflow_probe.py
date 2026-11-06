"""Real stdio probe for capabilities and read-only recovery/response behavior.

No input is injected. The rejection probe explicitly uses dry_run=True with an
invalid observation ID. Device access requires --serial from fresh discovery.
"""
import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys

from mcp import Client, StdioServerParameters


ROOT = Path(__file__).resolve().parents[1]


async def run(args):
    report = {'time': datetime.now(timezone.utc).isoformat(), 'phone_inputs': 0, 'calls': []}
    params = StdioServerParameters(command=sys.executable, args=['-m', 'mobile_use.server'], cwd=ROOT,
                                   env=dict(os.environ, PYTHONIOENCODING='utf-8'))
    async with Client(params, read_timeout_seconds=60) as client:
        async def call(name, arguments=None, *, expected_error=False):
            result = await client.call_tool(name, arguments or {})
            data = result.structured_content or {}
            report['calls'].append({'tool': name, 'is_error': result.is_error,
                                    'result_bytes': len(result.model_dump_json(by_alias=True).encode('utf-8')),
                                    'timings': data.get('timings'), 'wait': data.get('wait'),
                                    'recovery': data.get('recovery')})
            if result.is_error != expected_error:
                raise RuntimeError(data.get('error', 'Unexpected MCP error status'))
            return data

        report['tools'] = [t.name for t in (await client.list_tools()).tools]
        status = await call('get_status')
        assert status['capabilities']['action_until'] and 'wait_for_state' in report['tools']
        report['server_version'] = status['server_version']
        devices = (await call('list_devices'))['devices']
        report['devices'] = devices
        if args.serial:
            if args.serial not in [d['serial'] for d in devices]:
                raise RuntimeError('Choose an exact online serial from fresh discovery')
            report['connection'] = await call('connect', {'serial': args.serial})
            try:
                obs = (await call('observe', {'image': 'never'}))['observation']
                assert obs['actionable']
                focused = (await call('get_observation', {'observation_id': obs['observation_id'],
                    'image': 'never', 'detail': 'compact',
                    'focus': {'bounds': [0, 0, obs['width'], obs['height']], 'max_elements': 3}}))['observation']
                assert focused['observation_id'] == obs['observation_id']
                assert focused['capture'] == obs['capture'] and focused['returned_element_count'] <= 3
                assert focused['elements_filtered']
                waited = await call('wait_for_state', {'package': obs['package'], 'timeout_seconds': 3, 'image': 'never'})
                assert waited['wait']['status'] == 'matched'
                rejected = await call('tap', {'observation_id': 'probe-intentionally-stale', 'x': 0, 'y': 0,
                                              'dry_run': True, 'image': 'never'}, expected_error=True)
                assert rejected['input_status'] == 'not_sent'
                assert rejected['recovery']['status'] == 'observed'
                assert rejected['observation']['observation_id'] != waited['observation']['observation_id']
                assert not rejected['recovery']['input_replayed']
                report['status'] = 'passed'
            except Exception as exc:
                report.update(status='blocked', error=str(exc))
            finally:
                await call('disconnect')
    report.setdefault('status', 'discovery_passed')
    destination = ROOT / 'artifacts' / ('workflow-probe-' + datetime.now().strftime('%Y%m%d-%H%M%S') + '.json')
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(destination)
    return report['status'] != 'blocked'


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--serial')
    sys.exit(0 if asyncio.run(run(parser.parse_args())) else 1)
