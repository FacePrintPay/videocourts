#!/usr/bin/env python3
"""Unified build entry point for VideoCourts with State, Federal & Vendor PaaS/SaaS compliance."""

import argparse
import hashlib
import json
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRV_PATH = ROOT / "api/server.py"
UI_PATH = ROOT / "videocourts/index.html"
BUNDLE_PATH = ROOT / "data/full_compiled_build.json"
BACKUP_DIR = Path.home() / "tmp/vc_backups"
LOG_PATH = BACKUP_DIR / "aimetaverse_8000.log"


def sha256_file(p: Path) -> str:
    if not p.exists():
        return ""
    return hashlib.sha256(p.read_bytes()).hexdigest()


def compliance_baseline() -> dict:
    return {
        "paas_saas_controls": {
            "transport_encryption": "TLS 1.2+ required in transit (HTTPS/WSS reverse proxy in production)",
            "storage_encryption": "Encryption at rest required for sensitive/regulated data stores",
            "identity_and_access": "MFA/SSO support, least-privilege API keys, quarterly privilege review, no raw biometric templates",
            "audit_and_forensics": "Tamper-evident SHA-256 hash chain + exportable security/access logs",
            "data_lifecycle": "Court-authoritative off-chain custody for PII/sealed records; defined retention and purge/export controls",
        },
        "state_and_judicial_alignment": {
            "court_custody_and_sealing": "Courts retain authoritative case/PII records; redaction and sealed-filing rules respected",
            "interoperability_standards": "Align schemas and exchanges with court/justice standards (NIEM, OASIS ECF, state e-filing rules)",
        },
        "technology_vendor_requirements": {
            "independent_assurance": "Current SOC 2 Type II / ISO 27001 / annual pen-test attestation tracking for sub-vendors",
            "no_unauthorized_ai_training": "Strict prohibition on training external/commercial models on confidential legal or user data",
            "incident_response_sla": "Rapid breach notification commitment (24-72h target escalation) and subprocessor flow-down terms",
            "data_residency_and_dsp": "Approved jurisdictional residency and screening for covered/restricted data transactions (DOJ DSP / CISA security rules)",
        },
    }



NC_PILOT_TEMPLATE = {
    "jurisdiction_code": "US-NC",
    "jurisdiction_name": "North Carolina (Default Pilot Template)",
    "is_nc_pilot_template": True,
    "template_fallback": True,
    "editable_by_court": True,
    "statutory_reference": "N.C.G.S. § 7A-49.6 / AOC remote hearing procedures",
    "rules": {
        "recording_prohibited": True,
        "requires_judicial_or_court_approval": True,
        "party_consent_required_default": False,
        "private_counsel_consultation_enabled": True,
        "remote_witness_oath_verification": True,
        "public_observation_mode": "court_controlled",
        "pii_offchain_court_authoritative": True,
        "blockchain_hash_only_anchoring": True,
    },
}


def resolve_jurisdiction_rules(jurisdiction_code: str = "US-NC", overrides_path: Path | None = None) -> dict:
    code = (jurisdiction_code or "US-NC").strip().upper()
    overrides = {}
    if overrides_path and overrides_path.exists():
        try:
            overrides = json.loads(overrides_path.read_text(encoding="utf-8"))
        except Exception:
            overrides = {}

    custom_map = overrides.get("jurisdictions") if isinstance(overrides, dict) else {}
    if isinstance(custom_map, dict) and code in custom_map and isinstance(custom_map[code], dict):
        merged = json.loads(json.dumps(NC_PILOT_TEMPLATE))
        merged["jurisdiction_code"] = code
        merged["jurisdiction_name"] = custom_map[code].get("jurisdiction_name", code)
        merged["template_fallback"] = False
        merged["rules"].update(custom_map[code].get("rules") or {})
        return merged

    out = json.loads(json.dumps(NC_PILOT_TEMPLATE))
    out["jurisdiction_code"] = code
    if code != "US-NC":
        out["jurisdiction_name"] = f"{code} (Using NC Pilot Template Fallback)"
        out["template_fallback"] = True
    else:
        out["jurisdiction_name"] = "North Carolina (NC Pilot Template)"
        out["template_fallback"] = False
    return out


def run_build(start_server: bool = False) -> dict:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    if SRV_PATH.exists():
        subprocess.run([sys.executable, "-m", "py_compile", str(SRV_PATH)], check=True)

    srv_txt = SRV_PATH.read_text(encoding="utf-8", errors="ignore") if SRV_PATH.exists() else ""
    ui_txt = UI_PATH.read_text(encoding="utf-8", errors="ignore") if UI_PATH.exists() else ""

    get_sec, post_sec = (srv_txt.split("def do_POST", 1) + [""])[:2]
    get_routes = sorted(set(re.findall(r'path\s*==\s*["\'](/[^"\']+)["\']', get_sec)))
    post_routes = sorted(set(re.findall(r'path\s*==\s*["\'](/[^"\']+)["\']', post_sec)))
    cards = re.findall(r'<div class="card" id="([^"]+)">\s*<div class="card-header"><span>([^<]+)</span>', ui_txt)
    ui_modules = [{"id": cid, "title": title.replace("&amp;", "&")} for cid, title in cards]

    bundle_courts = 0
    if BUNDLE_PATH.exists():
        try:
            bdata = json.loads(BUNDLE_PATH.read_text(encoding="utf-8", errors="ignore"))
            bundle_courts = len(bdata.get("courts") or [])
        except Exception:
            pass

    manifest = {
        "platform": "VideoCourts Enterprise",
        "version": "2026.11-build-entry",
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S %Z"),
        "runtime": {
            "server": "Python ThreadingHTTPServer",
            "bind": "127.0.0.1:8000",
            "entrypoint": "build_entry.py -> api/server.py",
            "frontend": "videocourts/index.html",
        },
        "compliance": compliance_baseline(),
        "artifacts": {
            "server_py": {"path": "api/server.py", "sha256": sha256_file(SRV_PATH), "bytes": SRV_PATH.stat().st_size if SRV_PATH.exists() else 0},
            "index_html": {"path": "videocourts/index.html", "sha256": sha256_file(UI_PATH), "bytes": UI_PATH.stat().st_size if UI_PATH.exists() else 0},
            "compiled_build_json": {"path": "data/full_compiled_build.json", "sha256": sha256_file(BUNDLE_PATH), "compiled_courts": bundle_courts},
        },
        "endpoints": {"GET": get_routes, "POST": post_routes},
        "ui_modules": ui_modules,
    }

    manifest_file = ROOT / "PLATFORM_MANIFEST.json"
    manifest_file.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    doc_lines = [
        "# VideoCourts Build Entry & Compliance Documentation",
        "",
        f"**Build Entry:** `python3 build_entry.py` | **Version:** `{manifest['version']}` | **Generated:** `{manifest['generated_at']}`",
        "",
        "## 1. PaaS & SaaS Security Controls",
        *[f"- **{k}:** {v}" for k, v in manifest["compliance"]["paas_saas_controls"].items()],
        "",
        "## 2. State & Judicial Standards Alignment",
        *[f"- **{k}:** {v}" for k, v in manifest["compliance"]["state_and_judicial_alignment"].items()],
        "",
        "## 3. Technology Vendor & Data Security Requirements",
        *[f"- **{k}:** {v}" for k, v in manifest["compliance"]["technology_vendor_requirements"].items()],
        "",
        "## 4. Artifacts & Endpoints",
        f"- **GET routes ({len(get_routes)}):** {', '.join(get_routes) if get_routes else 'none detected'}",
        f"- **POST routes ({len(post_routes)}):** {', '.join(post_routes) if post_routes else 'none detected'}",
        f"- **UI modules ({len(ui_modules)}):** {', '.join(m['id'] for m in ui_modules) if ui_modules else 'none detected'}",
    ]
    doc_file = ROOT / "PLATFORM_DOCUMENTATION.md"
    doc_file.write_text("\n".join(doc_lines) + "\n", encoding="utf-8")

    if start_server and SRV_PATH.exists():
        import os
        for line in subprocess.run(["ps", "-ef"], capture_output=True, text=True).stdout.splitlines():
            if "api/server.py" in line and "build_entry.py" not in line and "python" in line:
                parts = line.split()
                if len(parts) > 1 and parts[1].isdigit() and int(parts[1]) != os.getpid():
                    try:
                        os.kill(int(parts[1]), 15)
                    except ProcessLookupError:
                        pass
        time.sleep(1)
        with LOG_PATH.open("ab") as log_f:
            subprocess.Popen(
                [sys.executable, str(SRV_PATH)],
                cwd=str(ROOT),
                stdout=log_f,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
        time.sleep(2)

    return {"manifest": str(manifest_file), "docs": str(doc_file), "started_server": start_server}


def main() -> None:
    parser = argparse.ArgumentParser(description="VideoCourts build entry point")
    parser.add_argument("--start", action="store_true", help="Start or restart api/server.py on port 8000 after build")
    args = parser.parse_args()
    res = run_build(start_server=args.start)
    print(json.dumps(res, indent=2))


if __name__ == "__main__":
    main()
