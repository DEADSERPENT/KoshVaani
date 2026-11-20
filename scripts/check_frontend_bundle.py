"""Scan the built frontend for things that must never reach a browser. Usage (from the repo root):

    cd frontend
    VITE_API_BASE=https://api.example.test npm run build && VITE_API_BASE=https://api.example.test npm run build:ops
    python ../scripts/check_frontend_bundle.py

Exits 1 if any rule fails. Run it in CI before every deploy."""
import re
import sys
from pathlib import Path

FE = Path(__file__).resolve().parents[1] / "frontend"
FAILS: list[str] = []


def text(build: Path) -> str:
    return "\n".join(p.read_text(encoding="utf-8", errors="ignore") for p in sorted((build / "assets").glob("*.js")))


def check(ok: bool, label: str) -> None:
    print(("PASS  " if ok else "FAIL  ") + label)
    if not ok:
        FAILS.append(label)


def scan(name: str, build: Path, must_not: list[str], must_not_re: list[str], must_have_html: list[str]) -> None:
    if not build.exists():
        check(False, f"{name}: build folder {build.name}/ exists (run the build first)")
        return
    js, html = text(build), (build / "index.html").read_text(encoding="utf-8")
    print(f"\n[{name}] {len(js):,} bytes of JS")
    for s in must_not:
        check(s not in js, f"{name}: bundle does not contain {s!r}")
    for r in must_not_re:
        check(not re.search(r, js), f"{name}: bundle has no match for /{r}/")
    for s in must_have_html:
        check(s in html, f"{name}: index.html contains {s!r}")
    check(not list(build.rglob("*.map")), f"{name}: no source maps shipped")
    check("api.example" in html or "connect-src" in html, f"{name}: CSP connect-src is pinned")
    check("unsafe-eval" not in html and "script-src 'self'" in html, f"{name}: scripts limited to self, no eval")


COMMON_BAD = ["localStorage", "console.log", "dev_code", "phone_masked", "Local simulation", "setItem(", "getItem("]
FIXTURE_PHONES = [r"99000001\d\d", r"\+919900"]

scan("customer", FE / "dist",
     must_not=COMMON_BAD + ["sessionStorage", "Operator sign-in", "Operations console", "dev panel", "/ops/auth", "kv_operator_session"],
     must_not_re=FIXTURE_PHONES,
     must_have_html=["Content-Security-Policy", "default-src 'none'", 'name="referrer"'])
scan("ops", FE / "dist-ops",
     must_not=COMMON_BAD + ["Enter your registered mobile", "getUserMedia", "MediaRecorder", "client.confirmation",
                            "/auth/challenges"],  # ops may only *remove* the legacy token key
     must_not_re=FIXTURE_PHONES,
     must_have_html=["Content-Security-Policy", "default-src 'none'"])

print(f"\n{len(FAILS)} failure(s)")
sys.exit(1 if FAILS else 0)
