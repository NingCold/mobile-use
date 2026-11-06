"""Real stdio MCP smoke probe. Phone mutations require explicit CLI flags."""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import sys
from pathlib import Path

from mcp import Client, StdioServerParameters

ROOT = Path(__file__).resolve().parents[1]


async def run(args):
    out = ROOT / "artifacts" / "debug"
    out.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    params = StdioServerParameters(command=sys.executable, args=["-m", "mobile_use.server"], cwd=ROOT, env=env)
    async with Client(params, read_timeout_seconds=60) as client:
        tools = await client.list_tools()
        print("TOOLS", ", ".join(t.name for t in tools.tools), flush=True)

        async def call(name, values=None, label="latest", expected_error=False):
            values = dict(values or {})
            # This legacy probe stores screenshots and indexes full element fields.
            if name in {"observe", "get_observation", "tap", "swipe", "type_text", "press_key", "launch_app", "wait_for"}:
                values.update(image="always", detail="full")
            result = await client.call_tool(name, values)
            if result.is_error:
                message = " ".join(c.text for c in result.content if c.type == "text")
                if expected_error:
                    print("EXPECTED_ERROR", message, flush=True)
                    return message
                raise RuntimeError(f"{name}: {message}")
            if expected_error:
                raise AssertionError(f"{name} unexpectedly succeeded")
            data = result.structured_content
            if data is None:
                data = json.loads(next(c.text for c in result.content if c.type == "text"))
            json_label = label if "observation" in data else name
            (out / f"{json_label}.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            for block in result.content:
                if block.type == "image":
                    (out / f"{label}.png").write_bytes(base64.b64decode(block.data))
            obs = data.get("observation")
            if obs:
                print(json.dumps({"call": name, "package": obs["package"], "observation_id": obs["observation_id"],
                                  "elements": len(obs["elements"]), "stable": obs["stable"],
                                  "png": str(out / f"{label}.png"),
                                  "execution_wall_ms": data.get("execution_wall_ms")}, ensure_ascii=False), flush=True)
                if not obs["actionable"]:
                    raise RuntimeError("Diagnostic observation only; reobserve before input, never replay")
            else:
                print(name, json.dumps(data, ensure_ascii=False), flush=True)
            return data

        await call("list_devices")
        if not args.serial:
            return
        await call("connect", {"serial": args.serial})
        try:
            data = await call("observe", label="before")
            if args.launch:
                data = await call("launch_app", {"observation_id": data["observation"]["observation_id"],
                                                "package": args.launch}, label="launched")
            if args.expect_package and data["observation"]["package"] != args.expect_package:
                raise AssertionError("Unexpected foreground app; no inputs sent")
            if args.tap_xy:
                x, y = args.tap_xy
                data = await call("tap", {"observation_id": data["observation"]["observation_id"],
                                          "x": x, "y": y, "seed": 123}, label="coordinate-tap")
            if args.calculator_test:
                if data["observation"]["package"] != "com.miui.calculator":
                    raise AssertionError("Open the authorized Xiaomi calculator before this probe")
                steps = ["btn_c_s", "digit_1", "digit_2", "op_add", "digit_3", "digit_4", "btn_equal_s"]
                for i, rid in enumerate(steps):
                    obs = data["observation"]
                    matches = [e for e in obs["elements"] if e["resource_id"] == f"com.miui.calculator:id/{rid}"]
                    if len(matches) != 1:
                        raise AssertionError(f"Expected one visible {rid}")
                    data = await call("tap", {"observation_id": obs["observation_id"], "element_id": matches[0]["id"],
                                              "seed": i + 40}, label=f"calc-{i}")
            if args.calculator_test or args.verify_result:
                # Xiaomi can temporarily expose only a first-use tooltip in its
                # accessibility tree. Wait for the result; NEVER repeat the equals tap.
                data = await call("wait_for", {"text": "46", "timeout_seconds": 15}, label="calculator-result")
                values = [e["text"].replace(",", "").replace(" ", "").lstrip("=")
                          for e in data["observation"]["elements"]
                          if e["resource_id"].endswith(("/result", "/expression"))]
                assert "46" in values, f"Calculation did not produce 46: {values}"
                old = data["observation"]["observation_id"]
                data = await call("observe", label="calculator-result")
                await call("tap", {"observation_id": old, "x": 10, "y": 10}, expected_error=True)
                await call("stop")
                await call("tap", {"observation_id": data["observation"]["observation_id"], "x": 10, "y": 10}, expected_error=True)
                data = await call("observe", {"resume": True}, label="calculator-result")
                print("PASS: 12 + 34 = 46; stale-state rejection; stop/resume", flush=True)
            if args.tap_resource:
                obs = data["observation"]
                matches = [e for e in obs["elements"] if e["resource_id"] == args.tap_resource]
                assert len(matches) == 1, "Resource must match exactly one observed element"
                data = await call("tap", {"observation_id": obs["observation_id"], "element_id": matches[0]["id"], "seed": 123})
            if args.tap_text:
                obs = data["observation"]
                matches = [e for e in obs["elements"] if e["text"] == args.tap_text]
                assert len(matches) == 1, "Text must match exactly one observed element"
                data = await call("tap", {"observation_id": obs["observation_id"], "element_id": matches[0]["id"], "seed": 123})
            if args.swipe:
                sx, sy, ex, ey = args.swipe
                data = await call("swipe", {"observation_id": data["observation"]["observation_id"],
                                           "start_x": sx, "start_y": sy, "end_x": ex, "end_y": ey,
                                           "kind": args.kind, "seed": 123}, label="swipe-result")
            if args.key:
                data = await call("press_key", {"observation_id": data["observation"]["observation_id"], "key": args.key})
            if args.text is not None:
                data = await call("type_text", {"observation_id": data["observation"]["observation_id"], "text": args.text})
        finally:
            await call("disconnect")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser()
    p.add_argument("--serial")
    p.add_argument("--calculator-test", action="store_true")
    p.add_argument("--verify-result", action="store_true")
    p.add_argument("--tap-resource")
    p.add_argument("--launch")
    p.add_argument("--expect-package")
    p.add_argument("--tap-xy", type=int, nargs=2)
    p.add_argument("--tap-text")
    p.add_argument("--swipe", type=int, nargs=4)
    p.add_argument("--kind", choices=["browse", "fling", "drag"], default="browse")
    p.add_argument("--key", choices=["back", "home", "enter", "delete", "recent"])
    p.add_argument("--text")
    asyncio.run(run(p.parse_args()))
