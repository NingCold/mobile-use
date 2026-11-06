"""Build a source-only plugin ZIP; never include device captures, logs or dependencies."""
from pathlib import Path
import json
import zipfile

root = Path(__file__).resolve().parents[1]
manifest = json.loads((root / "plugin.json").read_text(encoding="utf-8"))
assert manifest["name"] == "mobile-use"
assert len(manifest["extensions"]["com.openai"]["interface"]["shortDescription"]) <= 30
json.loads((root / "mcp.json").read_text(encoding="utf-8"))
destination = root.parent / f"mobile-use-{manifest['version']}.zip"
files = [root / p for p in ("plugin.json", "mcp.json", "README.md", "VALIDATION.md", "docs/install-for-agents.md",
                            "LICENSE", "pyproject.toml", "uv.lock", ".gitignore")]
for folder, pattern in (("src/mobile_use", "*.py"), ("skills/mobile-use", "*.md"),
                        ("scripts", "*.py"), ("tests", "*.py")):
    files.extend(p for p in (root / folder).rglob(pattern) if p.is_file() and not p.is_symlink()
                 and not any(part.startswith(".") or part == "__pycache__" for part in p.relative_to(root).parts))
with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
    for file in sorted(files):
        archive.write(file, "mobile-use/" + file.relative_to(root).as_posix())
with zipfile.ZipFile(destination) as archive:
    assert archive.testzip() is None
    assert not any("artifacts/" in n or ".venv/" in n for n in archive.namelist())
print(json.dumps({"archive": str(destination), "files": len(files), "bytes": destination.stat().st_size}, indent=2))
