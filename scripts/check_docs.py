"""Check local documentation links and immutable synthetic demo bundles, without network."""

import re
from pathlib import Path

from llm_test_lab.evidence import verify_bundle

ROOT = Path(__file__).resolve().parents[1]
errors: list[str] = []
for path in [ROOT / "README.md", *(ROOT / "docs").rglob("*.md")]:
    for raw in re.findall(r"\[[^\]]*\]\(([^)]+)\)", path.read_text()):
        target = raw.split("#", 1)[0]
        if not target or "://" in target or target.startswith("mailto:"):
            continue
        resolved = (path.parent / target).resolve()
        if not resolved.is_relative_to(ROOT) or not resolved.exists():
            errors.append(f"{path.relative_to(ROOT)}: broken local link {raw}")
for manifest in (ROOT / "examples/results").glob("*/manifest.json"):
    errors.extend(f"{manifest.parent.name}: {error}" for error in verify_bundle(manifest.parent))
if errors:
    raise SystemExit("\n".join(errors))
print("Local documentation links and saved synthetic bundles: OK (no network)")
