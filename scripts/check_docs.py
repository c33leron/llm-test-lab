"""Check local documentation links and immutable synthetic demo bundles, without network."""

import re
from pathlib import Path

from package_source import source_files

from llm_test_lab.evidence import verify_bundle

ROOT = Path(__file__).resolve().parents[1]
errors: list[str] = []
published = source_files(ROOT)
public_paths = {p.resolve() for p in published}
for path in (p for p in published if p.suffix == ".md"):
    for raw in re.findall(r"\[[^\]]*\]\(([^)]+)\)", path.read_text()):
        target = raw.split("#", 1)[0]
        if not target or "://" in target or target.startswith("mailto:"):
            continue
        resolved = (path.parent / target).resolve()
        visible = resolved in public_paths or (
            resolved.is_dir() and any(p.is_relative_to(resolved) for p in public_paths)
        )
        if not resolved.is_relative_to(ROOT) or not resolved.exists() or not visible:
            errors.append(f"{path.relative_to(ROOT)}: broken local link {raw}")
for manifest in (ROOT / "examples/results").glob("*/manifest.json"):
    errors.extend(f"{manifest.parent.name}: {error}" for error in verify_bundle(manifest.parent))
if errors:
    raise SystemExit("\n".join(errors))
print("Local documentation links and saved synthetic bundles: OK (no network)")
