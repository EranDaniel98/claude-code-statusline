"""Print all four status indicator states so you can see the colors rendered.

Run: python ~/.claude/tests/test_colors.py
"""
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


def ansi(text: str, code: str) -> str:
    return f"\x1b[{code}m{text}\x1b[0m"


print()
print(f"  {ansi('●', '1;32')}    green   — session busy (Claude is working)")
print(f"  {ansi('WAIT', '1;31')}  red    — permission prompt waiting")
print(f"  (none) — idle session")
print(f"  elapsed segment turns red ≥2min for the stuck signal")
print()
