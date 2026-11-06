"""Read-only stdio probe for image policies and same-observation expansion.

No phone input or app launch. Without --serial only discovers tools/devices.
Writes size/timing metadata locally, never screenshot bytes or UI text.
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
    params = StdioServerParameters(command=sys.executable, args=["-m", "mobile_use.server"],
                                   cwd=ROOT, env=dict(os.environ, PYTHONIOENCODING="utf-8"))
    report = {"time": datetime.now(timezone.utc).isoformat(), "phone_inputs": 0, "samples": []}
    async with Client(params, read_timeout_seconds=60) as client:
        async def call(name, arguments=None):
            result = await client.call_tool(name, arguments or {})
            if result.is_error:
                raise RuntimeError(str(result.structured_content or [c.text for c in result.content if c.type == "text"]))
            return result
        report["tools"] = [t.name for t in (await client.list_tools()).tools]
        assert "get_observation" in report["tools"]
        devices = (await call("list_devices")).structured_content["devices"]
        report["devices"] = devices
        if args.serial:
            if args.serial not in [d["serial"] for d in devices]:
                raise RuntimeError("Choose an exact online serial from discovery")
            report["connection"] = (await call("connect", {"serial": args.serial})).structured_content
            try:
                for _ in range(3):
                    captured = await call("observe", {"image": "always", "detail": "compact"})
                    obs = captured.structured_content["observation"]
                    oid = obs["observation_id"]
                    compact = await call('get_observation', {'observation_id':oid,'image':'never','detail':'compact'})
                    full = await call("get_observation", {"observation_id": oid, "image": "always", "detail": "full"})
                    auto = await call("get_observation", {"observation_id": oid, "image": "auto", "detail": "compact"})
                    assert not any(c.type == "image" for c in compact.content)
                    assert sum(c.type == "image" for c in full.content) == 1
                    for r in (full, auto):
                        assert r.structured_content["observation"]["observation_id"] == oid
                        assert r.structured_content["observation"]["capture"] == obs["capture"]
                    size = lambda r: len(r.model_dump_json(by_alias=True).encode("utf-8"))
                    report["samples"].append({"observation_id": oid, "actionable": obs["actionable"],
                        "elements": obs["element_count"], "image_reasons": obs["image_reasons"],
                        "auto_included_image": any(c.type == "image" for c in auto.content),
                        "compact_never_result_bytes": size(compact), "full_always_result_bytes": size(full),
                        "compact_auto_result_bytes": size(auto), "capture": obs["capture"]})
                text_only=await call('observe',{'image':'never'})
                obs=text_only.structured_content['observation']
                assert not obs['image']['available']
                assert text_only.structured_content['reads'].get('screenshot',0)==0
                missing=await client.call_tool('get_observation',{'observation_id':obs['observation_id'],'image':'always'})
                assert missing.is_error and not missing.structured_content.get('observation')
                report['true_text_only_verified']=True
            except RuntimeError as exc:
                report["status"] = "blocked"
                report["error"] = str(exc)
            finally:
                await call("disconnect")
    report.setdefault("status", "passed")
    destination = ROOT / "artifacts/observation-probe.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(destination)
    return report["status"] == "passed"


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serial")
    sys.exit(0 if asyncio.run(run(parser.parse_args())) else 1)
