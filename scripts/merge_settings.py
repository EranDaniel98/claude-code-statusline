"""Deep-merge a JSON snippet into ~/.claude/settings.json.

- Backs up the existing file to settings.json.bak (overwriting any previous bak).
- Deep-merges objects key-by-key. Arrays append uniquely (no duplicate hook entries).
- Refuses to clobber an existing statusLine if it points elsewhere — prints a warning
  so the user can decide.

Usage:
  python merge_settings.py <path-to-snippet.json>
  python merge_settings.py <path-to-snippet.json> --settings <path-to-settings.json>
"""
import argparse
import json
import shutil
import sys
from pathlib import Path


def deep_merge(target: dict, source: dict) -> dict:
    """Merge source into target. Returns target. Dicts merge recursively;
    lists are concatenated with duplicate items skipped."""
    for key, src_val in source.items():
        if key not in target:
            target[key] = src_val
            continue
        tgt_val = target[key]
        if isinstance(tgt_val, dict) and isinstance(src_val, dict):
            deep_merge(tgt_val, src_val)
        elif isinstance(tgt_val, list) and isinstance(src_val, list):
            for item in src_val:
                if item not in tgt_val:
                    tgt_val.append(item)
        else:
            target[key] = src_val
    return target


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("snippet", help="Path to JSON snippet to merge in")
    ap.add_argument("--settings", default=None,
                    help="Path to settings.json (default: ~/.claude/settings.json)")
    args = ap.parse_args()

    settings_path = Path(args.settings) if args.settings else (
        Path.home() / ".claude" / "settings.json"
    )
    snippet_path = Path(args.snippet)

    if not snippet_path.exists():
        print(f"error: snippet not found: {snippet_path}", file=sys.stderr)
        return 1

    snippet = json.loads(snippet_path.read_text(encoding="utf-8"))

    settings_path.parent.mkdir(parents=True, exist_ok=True)

    if settings_path.exists():
        existing = json.loads(settings_path.read_text(encoding="utf-8"))
        backup = settings_path.with_suffix(settings_path.suffix + ".bak")
        shutil.copy2(settings_path, backup)
        print(f"backed up: {backup}")

        existing_sl = existing.get("statusLine")
        snippet_sl = snippet.get("statusLine")
        if existing_sl and snippet_sl and existing_sl != snippet_sl:
            print("warning: existing statusLine differs from snippet — overwriting.",
                  file=sys.stderr)
            print(f"  was:  {json.dumps(existing_sl)}", file=sys.stderr)
            print(f"  now:  {json.dumps(snippet_sl)}", file=sys.stderr)
    else:
        existing = {}

    merged = deep_merge(existing, snippet)
    settings_path.write_text(
        json.dumps(merged, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"wrote: {settings_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
