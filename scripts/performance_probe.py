"""Read-only same-device legacy/target comparison; never injects input."""
import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from mcp import Client, StdioServerParameters
from mobile_use.options import seconds


def dwell_seconds(value):
    try:
        return seconds(float(value), '--dwell-seconds', allow_zero=True)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None

async def run(args):
    root=Path(__file__).resolve().parents[1]
    events=[]
    output_options={'image':'never'}
    if args.focus_text is not None:
        output_options['focus']={'text':args.focus_text}
    for mode in ('legacy','target'):
        params=StdioServerParameters(command=sys.executable,
            args=['-m','mobile_use.server','--validation-mode',mode],cwd=root,
            env=dict(os.environ,PYTHONIOENCODING='utf-8'))
        async with Client(params,read_timeout_seconds=60) as client:
            async def call(tool,arguments=None):
                start=time.perf_counter()
                r=await client.call_tool(tool,arguments or {})
                record={'mode':mode,'tool':tool,'wall_ms':round((time.perf_counter()-start)*1000),
                        'is_error':r.is_error,'data':r.structured_content}
                events.append(record)
                args.output.write_text(json.dumps(events,ensure_ascii=False,indent=2),encoding='utf-8')
                return record['data']
            devices=await call('list_devices')
            assert args.serial in [d['serial'] for d in devices['devices']]
            await call('connect',{'serial':args.serial})
            try:
                for _ in range(3):
                    obs=await call('observe',output_options)
                if mode=='target' and args.dwell_seconds and 'observation' in obs:
                    o=obs['observation']
                    await call('wait_for_state',{'observation_id':o['observation_id'],'package':o['package'],
                        'duration_seconds':args.dwell_seconds,'timeout_seconds':min(30,args.dwell_seconds+5),
                        **output_options})
            finally:
                await call('disconnect')
    print(json.dumps([{'mode':e['mode'],'tool':e['tool'],'wall_ms':e['wall_ms'],'error':e['is_error'],
                       'reads':e['data'].get('reads'),'timings':e['data'].get('timings')}
                      for e in events if e['tool'] in ('observe','wait_for_state')],ensure_ascii=False,indent=2))
if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--serial',required=True)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--focus-text',help='Optional observed text to filter returned elements; no app-specific default')
    p.add_argument('--dwell-seconds',type=dwell_seconds,default=0,
                   help='Optional read-only foreground dwell (0..30 seconds); 0 skips it')
    asyncio.run(run(p.parse_args()))
