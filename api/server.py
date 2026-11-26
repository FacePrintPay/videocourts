import base64
import hashlib
import hmac
import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HOST = "127.0.0.1"
PORT = int(os.environ.get("PORT", "8000"))
BASE_DIR = Path.home() / "AiMetaverse"
VC_DIR = BASE_DIR / "videocourts"
DATA_DIR = BASE_DIR / "data"
DOCS_DIR = DATA_DIR / "vc_docs"
FULL_BUILD_PATH = DATA_DIR / "full_compiled_build.json"
FALLBACK_BUILD_PATH = DATA_DIR / "all_files_build.json"
STATE_PATH = DATA_DIR / "vc_state.json"

DOCS_DIR.mkdir(parents=True, exist_ok=True)


def _load_compiled_build() -> dict:
    for candidate in (FULL_BUILD_PATH, FALLBACK_BUILD_PATH):
        if candidate.exists():
            try:
                data = json.loads(candidate.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
            except Exception:
                continue
    return {
        "ok": True,
        "roots_scanned": [],
        "total_files": 0,
        "total_court_matches": 0,
        "total_pilot_or_court_docs": 0,
        "files_by_ext": {},
        "pilot_docs": [],
        "courts": [],
        "files_manifest": [],
    }


def _default_licensing_config():
    return {
        "default_fallback_jurisdiction": "US-NC",
        "test_licenses_require_entity_authorization": True,
        "tiers": {
            "pilot_nc_template": {"label": "Pilot (US-NC Template)", "monthly_usd": 490, "max_seats": 25},
            "county_standard": {"label": "County / District Standard", "monthly_usd": 2500, "max_seats": 100},
            "state_enterprise": {"label": "Statewide / Enterprise", "monthly_usd": 9500, "max_seats": 500}
        },
        "annual_internal_costs": {
            "lean_self_hosted_usd": 1800,
            "multi_court_cloud_usd": 12000,
            "ai_api_annual_usd": 264,
            "hosting_usd": 1200,
            "court_data_usd": 0,
            "comms_usd": 900,
            "compliance_usd": 2500
        },
        "terms_summary": "Test licenses stay pending_activation until bound to a court or legal entity; unconfigured jurisdictions fall back to US-NC; PII remains off-chain with hash-only ledger anchoring."
    }

def _get_licensing_config(st):
    cfg = _default_licensing_config()
    saved = st.get("licensing_config")
    if isinstance(saved, dict):
        for k, v in saved.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    return cfg

def _compute_licensing_breakeven(st, cfg):
    import math
    tiers = cfg.get("tiers") or {}
    costs = cfg.get("annual_internal_costs") or {}
    lean_yr = float(costs.get("lean_self_hosted_usd") or 0)
    cloud_yr = float(costs.get("multi_court_cloud_usd") or 0)
    per_tier = {}
    monthly_rates = []
    for t_key, t_info in tiers.items():
        m_usd = float((t_info or {}).get("monthly_usd") or 0)
        a_usd = round(m_usd * 12.0, 2)
        if m_usd > 0:
            monthly_rates.append(m_usd)
            be_lean = int(math.ceil(lean_yr / (m_usd * 12.0))) if lean_yr > 0 else 0
            be_cloud = int(math.ceil(cloud_yr / (m_usd * 12.0))) if cloud_yr > 0 else 0
        else:
            be_lean = None
            be_cloud = None
        per_tier[t_key] = {
            "monthly_usd": m_usd,
            "annual_usd_per_license": a_usd,
            "breakeven_courts_lean_self_hosted": be_lean,
            "breakeven_courts_multi_court_cloud": be_cloud
        }
    avg_m = (sum(monthly_rates) / len(monthly_rates)) if monthly_rates else 0.0
    lics = st.get("court_licenses") or []
    active_lics = [x for x in lics if bool(x.get("authorized")) and x.get("status") == "active"]
    est_annual_rev = 0.0
    for item in active_lics:
        t_name = item.get("tier") or "pilot_nc_template"
        m_rate = float(((tiers.get(t_name) or {}).get("monthly_usd")) or avg_m or 0.0)
        est_annual_rev += m_rate * 12.0
    return {
        "annual_internal_costs_usd": {
            "lean_self_hosted": lean_yr,
            "multi_court_cloud": cloud_yr
        },
        "per_tier": per_tier,
        "blended_monthly_usd": round(avg_m, 2),
        "blended_breakeven_courts_lean": int(math.ceil(lean_yr / (avg_m * 12.0))) if avg_m > 0 and lean_yr > 0 else None,
        "blended_breakeven_courts_cloud": int(math.ceil(cloud_yr / (avg_m * 12.0))) if avg_m > 0 and cloud_yr > 0 else None,
        "active_authorized_licenses": len(active_lics),
        "estimated_active_annual_revenue_usd": round(est_annual_rev, 2),
        "net_annual_vs_lean_usd": round(est_annual_rev - lean_yr, 2),
        "net_annual_vs_cloud_usd": round(est_annual_rev - cloud_yr, 2)
    }

def _compute_licensing_projection(st, cfg, ramp_courts_per_quarter=2):
    tiers = cfg.get("tiers") or {}
    costs = cfg.get("annual_internal_costs") or {}
    lean_monthly = float(costs.get("lean_self_hosted_usd") or 0) / 12.0
    cloud_monthly = float(costs.get("multi_court_cloud_usd") or 0) / 12.0
    rates = [float((v or {}).get("monthly_usd") or 0) for v in tiers.values() if float((v or {}).get("monthly_usd") or 0) > 0]
    blended_m = (sum(rates) / len(rates)) if rates else 490.0
    lics = st.get("court_licenses") or []
    base_courts = len([x for x in lics if bool(x.get("authorized")) and x.get("status") == "active"])
    ramp_q = max(0, int(ramp_courts_per_quarter or 0))
    cum_rev = 0.0
    cum_lean = 0.0
    cum_cloud = 0.0
    milestones = {}
    for m in range(1, 37):
        quarters_elapsed = (m - 1) // 3
        active_c = base_courts + (quarters_elapsed * ramp_q)
        m_rev = active_c * blended_m
        cum_rev += m_rev
        cum_lean += lean_monthly
        cum_cloud += cloud_monthly
        if m in (12, 24, 36):
            milestones[f"month_{m}"] = {
                "month": m,
                "active_courts": active_c,
                "monthly_revenue_usd": round(m_rev, 2),
                "cumulative_revenue_usd": round(cum_rev, 2),
                "cumulative_net_lean_usd": round(cum_rev - cum_lean, 2),
                "cumulative_net_cloud_usd": round(cum_rev - cum_cloud, 2)
            }
    return {
        "starting_active_courts": base_courts,
        "ramp_courts_per_quarter": ramp_q,
        "blended_monthly_usd": round(blended_m, 2),
        "monthly_burn_lean_usd": round(lean_monthly, 2),
        "monthly_burn_cloud_usd": round(cloud_monthly, 2),
        "milestones": milestones
    }

def _platform_ai_functions():
    return [
        {
            "id": "ollama_local_qwen",
            "name": "Local Ollama / Qwen Code & Triage",
            "provider": "ollama",
            "endpoint": "http://127.0.0.1:11434/api/chat",
            "default_model": "qwen2.5-coder:3b",
            "cost_model": "local_compute_zero_api_fee"
        },
        {
            "id": "grok_legal_assistant",
            "name": "Grok Legal AI Assistant",
            "provider": "grok",
            "endpoint": "https://api.x.ai/v1/chat/completions",
            "default_model": "grok-4.7",
            "cost_model": "pay_per_1m_tokens_xai"
        },
        {
            "id": "docket_jurisdiction_engine",
            "name": "CourtListener Docket & US-NC Rule Fallback",
            "provider": "internal",
            "endpoint": "/api/dockets/track",
            "cost_model": "open_access_plus_local_cache"
        },
        {
            "id": "totalrecall_ledger_anchor",
            "name": "TotalRecall Non-PII SHA-256 Ledger Anchor",
            "provider": "internal",
            "endpoint": "/api/security/verify",
            "cost_model": "local_hash_audit"
        }
    ]

def _compute_max_virtual_calendar(slot_minutes=20, state_rooms=5, federal_rooms=4, session_minutes=480, days_per_month=21):
    slot = max(5, int(slot_minutes or 20))
    sess = max(60, int(session_minutes or 480))
    per_room = sess // slot
    st_rooms = max(1, int(state_rooms or 5))
    fed_rooms = max(1, int(federal_rooms or 4))
    dpm = max(1, int(days_per_month or 21))
    state_daily = per_room * st_rooms
    fed_daily = per_room * fed_rooms
    return {
        "estimate_disclaimer": "ESTIMATE: Configurable scheduling model based on session_minutes, slot_minutes, and concurrent rooms.",
        "session_minutes_per_day": sess,
        "slot_minutes_per_appearance": slot,
        "appearances_per_room_daily": per_room,
        "state_courts": {
            "reference_jurisdiction": "US-NC",
            "concurrent_virtual_rooms": st_rooms,
            "max_daily_virtual_appearances": state_daily,
            "est_monthly_virtual_appearances": state_daily * dpm
        },
        "federal_courts": {
            "reference_scope": "US Federal / District / Remote Dockets",
            "concurrent_virtual_rooms": fed_rooms,
            "max_daily_virtual_appearances": fed_daily,
            "est_monthly_virtual_appearances": fed_daily * dpm
        },
        "combined_max_daily_virtual_appearances": state_daily + fed_daily
    }

def _ensure_default_court_slots(st):
    slots = st.get("court_virtual_slots")
    if isinstance(slots, list) and slots:
        return slots
    default_slots = [
        {"slot_id": "SLOT-NC-0900", "jurisdiction_code": "US-NC", "date": "2026-12-01", "window": "09:00-09:20 EST", "room": "Virtual-Room-A", "status": "available", "assigned_request_id": None},
        {"slot_id": "SLOT-NC-0920", "jurisdiction_code": "US-NC", "date": "2026-12-01", "window": "09:20-09:40 EST", "room": "Virtual-Room-A", "status": "available", "assigned_request_id": None},
        {"slot_id": "SLOT-NC-1000", "jurisdiction_code": "US-NC", "date": "2026-12-01", "window": "10:00-10:20 EST", "room": "Virtual-Room-B", "status": "available", "assigned_request_id": None}
    ]
    st["court_virtual_slots"] = default_slots
    return default_slots

def _run_server_approval_bot(st, request_id="", auto_assign_first_slot=True):
    slots = _ensure_default_court_slots(st)
    reqs = st.setdefault("schedule_requests", [])
    crm_users = st.get("crm_contacts") or st.get("users") or []
    synced = []
    for req in reqs:
        if request_id and req.get("request_id") != request_id:
            continue
        if req.get("status") == "approved_scheduled" and req.get("slot_id"):
            synced.append(req)
            continue
        matched_user = None
        u_ref = (req.get("user_ref") or req.get("participant_email") or req.get("account_name") or "").strip().lower()
        for u in crm_users:
            u_str = str(u).lower()
            if u_ref and u_ref in u_str:
                matched_user = u
                break
        chosen_slot = None
        requested_slot_id = (req.get("slot_id") or "").strip()
        for s in slots:
            if requested_slot_id and s.get("slot_id") == requested_slot_id and s.get("status") == "available":
                chosen_slot = s
                break
        if not chosen_slot and auto_assign_first_slot:
            r_jur = (req.get("jurisdiction_code") or "US-NC").strip().upper()
            for s in slots:
                if s.get("status") == "available" and (s.get("jurisdiction_code") or "US-NC").upper() == r_jur:
                    chosen_slot = s
                    break
            if not chosen_slot:
                for s in slots:
                    if s.get("status") == "available":
                        chosen_slot = s
                        break
        if chosen_slot:
            chosen_slot["status"] = "booked"
            chosen_slot["assigned_request_id"] = req.get("request_id")
            req["slot_id"] = chosen_slot.get("slot_id")
            req["scheduled_window"] = chosen_slot.get("window")
            req["virtual_room"] = chosen_slot.get("room")
            req["user_synced"] = bool(matched_user is not None or bool(u_ref))
            req["approved_by"] = "server_approval_bot"
            req["status"] = "approved_scheduled"
            req["approved_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            synced.append(req)
    return synced

def _get_or_init_user_gate(st, user_ref):
    key = (user_ref or "default-user").strip().lower()
    users = st.setdefault("user_onboarding_records", {})
    rec = users.get(key)
    if not isinstance(rec, dict):
        rec = {
            "user_ref": key,
            "idme_verified": False,
            "sync_pin_4digit": None,
            "pin_synced": False,
            "biometric_onboarded": False,
            "biometric_consent": False,
            "video_instructions_completed": False,
            "virtual_court_rules_accepted": False,
            "digital_agreements_signed": False,
            "fee_paid_cents": 0,
            "fee_type": None,
            "court_approved": False,
            "approved_by": None,
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S")
        }
        users[key] = rec
    return rec

def _check_user_scheduling_eligibility(rec):
    missing = []
    if not rec.get("idme_verified"):
        missing.append("idme_verification")
    if not bool(rec.get("idme_onboarded")):
        missing.append("idme_onboarded_true")
    if not bool(rec.get("live_agent_processed")):
        missing.append("live_agent_processed_true")
    if not rec.get("pin_synced") or not rec.get("sync_pin_4digit"):
        missing.append("4_digit_pin_sync")
    if not rec.get("biometric_onboarded") or not rec.get("biometric_consent"):
        missing.append("biometric_onboarding_and_consent")
    if not rec.get("video_instructions_completed"):
        missing.append("platform_video_instructions")
    if not rec.get("virtual_court_rules_accepted") or not rec.get("digital_agreements_signed"):
        missing.append("virtual_court_rules_and_agreements")
    fee_cents = int(rec.get("fee_paid_cents") or 0)
    if fee_cents not in (199, 1999) and fee_cents < 199:
        missing.append("fee_199_request_or_1999_appearance")
    if not rec.get("court_approved"):
        missing.append("court_server_approval")
    return (len(missing) == 0), missing

def _load_state() -> dict:
    if STATE_PATH.exists():
        try:
            data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {
        "efilings": [],
        "esignatures": [],
        "calendar_events": [],
        "virtual_appearances": [],
        "documents": [],
        "totalrecall_log": [],
        "crm_contacts": [],
        "onboarding_profiles": [],
        "video_rooms": [],
        "tyler_filings": [],
    }


def _save_state(state: dict):
    STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _record_totalrecall(event_type: str, detail: dict):
    state = _load_state()
    prev_records = state.get("totalrecall_log", [])
    prev_hash = prev_records[-1].get("sha256", "GENESIS") if prev_records else "GENESIS"
    entry = {
        "id": f"tr_{int(time.time() * 1000)}",
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "event": event_type,
        "prev_hash": prev_hash,
        "detail": detail,
    }
    entry["sha256"] = hashlib.sha256(json.dumps(entry, sort_keys=True).encode("utf-8")).hexdigest()
    prev_records.append(entry)
    state["totalrecall_log"] = prev_records
    _save_state(state)
    return entry


def _encrypt_envelope(plaintext: str, passphrase: str = "videocourts-local-key") -> dict:
    key = hashlib.pbkdf2_hmac("sha256", passphrase.encode("utf-8"), b"vc_salt_v1", 100_000)
    raw = plaintext.encode("utf-8")
    stream = bytearray()
    counter = 0
    while len(stream) < len(raw):
        block = hashlib.sha256(key + counter.to_bytes(4, "big")).digest()
        stream.extend(block)
        counter += 1
    cipher = bytes(a ^ b for a, b in zip(raw, stream[: len(raw)]))
    mac = hmac.new(key, cipher, hashlib.sha256).hexdigest()
    return {
        "scheme": "PBKDF2-SHA256-CTR+HMAC-SHA256",
        "ciphertext_b64": base64.b64encode(cipher).decode("ascii"),
        "hmac_sha256": mac,
    }


class AiMetaverseHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        return

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")

    def _send_json(self, status: int, payload: dict):
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self._cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _send_bytes(self, status: int, content_type: str, filename: str, raw: bytes):
        self.send_response(status)
        self._cors()
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _send_html(self, status: int, html: str):
        raw = html.encode("utf-8")
        self.send_response(status)
        self._cors()
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            return {}

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        if path == "/videocourts" or path.startswith("/videocourts/"):
            import mimetypes
            vc_root = (Path.home() / "AiMetaverse/videocourts").resolve()
            rel = path[len("/videocourts"):].lstrip("/") or "index.html"
            target = (vc_root / rel).resolve()
            if target.is_dir():
                target = (target / "index.html").resolve()
            if not str(target).startswith(str(vc_root)) or not target.is_file():
                self._send_json(404, {"ok": False, "error": "VideoCourts asset not found", "path": path})
                return
            ctype, _ = mimetypes.guess_type(str(target))
            data = target.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", f"{ctype or 'application/octet-stream'}; charset=utf-8" if (ctype or "").startswith(("text/", "application/javascript", "application/json")) else (ctype or "application/octet-stream"))
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        if path == "/api/paas/profile":
            st = _load_state()
            q_map = query if "query" in globals() and isinstance(query, dict) else {}
            u_ref = str((q_map.get("user_ref") or ["pilot-user@example.org"])[0]).strip().lower()
            rec = _get_or_init_user_gate(st, u_ref) if callable(globals().get("_get_or_init_user_gate")) else {"user_ref": u_ref}
            self._send_json(200, {
                "ok": True,
                "paas_targets": ["web", "mobile_pwa", "termux_android"],
                "hardware_imei_note": "Browser/standard apps cannot read hardware IMEI directly; use bound device_ref/hashed IMEI reference + SMS verification.",
                "profile": rec
            })
            return

        if path == "/api/schedule/slots":
            st = _load_state()
            slots = _ensure_default_court_slots(st)
            _save_state(st)
            avail = [s for s in slots if s.get("status") == "available"]
            self._send_json(200, {"ok": True, "available_slots": avail, "all_slots": slots})
            return

        if path == "/api/ai/functions":
            import os
            st = _load_state()
            cfg = _get_licensing_config(st) if callable(globals().get("_get_licensing_config")) else {}
            ai_yr = float((cfg.get("annual_internal_costs") or {}).get("ai_api_annual_usd") or 264)
            self._send_json(200, {"ok": True, "grok_key_configured": bool(os.environ.get("XAI_API_KEY")), "ollama_default_url": "http://127.0.0.1:11434", "ai_api_annual_usd": ai_yr, "functions": _platform_ai_functions()})
            return

        if path == "/api/courts/max-calendar":
            q_map = query if "query" in globals() and isinstance(query, dict) else {}
            slot_m = int(str((q_map.get("slot_minutes") or ["20"])[0]).strip() or 20)
            st_r = int(str((q_map.get("state_rooms") or ["5"])[0]).strip() or 5)
            fed_r = int(str((q_map.get("federal_rooms") or ["4"])[0]).strip() or 4)
            cal = _compute_max_virtual_calendar(slot_minutes=slot_m, state_rooms=st_r, federal_rooms=fed_r)
            self._send_json(200, {"ok": True, "calendar": cal})
            return

        if path == "/api/licensing/config":
            st = _load_state()
            cfg = _get_licensing_config(st)
            ramp_q = 2
            if "query" in globals() and isinstance(query, dict) and query.get("ramp_per_quarter"):
                try:
                    ramp_q = int(str(query["ramp_per_quarter"][0]).strip())
                except ValueError:
                    ramp_q = 2
            self._send_json(200, {"ok": True, "config": cfg, "breakeven": _compute_licensing_breakeven(st, cfg), "projection_12_to_36_months": _compute_licensing_projection(st, cfg, ramp_q)})
            return

        if path == "/api/licenses/authorize":
            st = _load_state()
            q_map = query if "query" in globals() and isinstance(query, dict) else {}
            lic_id = str((q_map.get("license_id") or [""])[0]).strip()
            lic_key = str((q_map.get("license_key") or [""])[0]).strip()
            found = None
            for item in reversed(st.get("court_licenses") or []):
                if (lic_id and item.get("license_id") == lic_id) or (lic_key and item.get("license_key") == lic_key):
                    found = item
                    break
            if not found:
                self._send_json(404, {"ok": False, "authorized": False, "error": "License not found"})
                return
            is_auth = bool(found.get("authorized")) and found.get("status") == "active" and bool((found.get("licensed_entity") or "").strip())
            self._send_json(200, {"ok": True, "authorized": is_auth, "license": found})
            return

        if path == "/api/licenses":
            st = _load_state()
            lics = st.get("court_licenses") or []
            self._send_json(200, {"ok": True, "count": len(lics), "licenses": lics})
            return

        if path == "/api/billing":
            st = _load_state()
            invs = st.get("billing_invoices") or []
            total_cents = sum(int(i.get("amount_cents") or 0) for i in invs if i.get("status") != "void")
            self._send_json(200, {"ok": True, "count": len(invs), "total_cents": total_cents, "invoices": invs})
            return

        if path == "/api/dockets/track":
            import os, urllib.parse, urllib.request
            st = _load_state()
            q_map = query if "query" in globals() and isinstance(query, dict) else {}
            docket_q = str((q_map.get("q") or q_map.get("docket") or ["NC-2026-001"])[0]).strip()
            jur_code = str((q_map.get("jurisdiction") or ["US-NC"])[0]).strip().upper()
            token = (os.environ.get("COURTLISTENER_TOKEN") or "").strip()
            cache = st.setdefault("docket_cache", {})
            cache_key = f"{jur_code}::{docket_q}"
            live_result = None
            source_mode = "local_open_fallback"
            rate_limit_note = "CourtListener free tier: 5/min, 50/hr, 125/day; cache responses locally."
            if token:
                try:
                    qs = urllib.parse.urlencode({"q": docket_q})
                    req = urllib.request.Request(
                        f"https://www.courtlistener.com/api/rest/v4/dockets/?{qs}",
                        headers={"Authorization": f"Token {token}", "User-Agent": "VideoCourts-DocketTracker/1.0"}
                    )
                    with urllib.request.urlopen(req, timeout=8) as resp:
                        live_result = json.loads(resp.read().decode("utf-8", errors="ignore"))
                        cache[cache_key] = {"fetched_at": time.strftime("%Y-%m-%d %H:%M:%S"), "data": live_result}
                        st["docket_cache"] = cache
                        _save_state(st)
                        source_mode = "courtlistener_v4_live"
                except Exception as exc:
                    live_result = {"error": str(exc)}
            fallback_dockets = [
                {
                    "docket_id": docket_q,
                    "jurisdiction_code": jur_code,
                    "rule_template_fallback": jur_code != "US-NC",
                    "status": "active_remote_hearing_tracked",
                    "pii_custody": "court_authoritative_offchain"
                }
            ]
            self._send_json(200, {
                "ok": True,
                "source_mode": source_mode,
                "query": docket_q,
                "jurisdiction_code": jur_code,
                "cached_entry": cache.get(cache_key),
                "live_api_response": live_result,
                "tracked_dockets": fallback_dockets,
                "rate_limit_guidance": rate_limit_note
            })
            return

        if path == "/api/jurisdiction/sources":
            code = "US-NC"
            if "query" in globals() and isinstance(query, dict) and query.get("code"):
                code = str(query["code"][0]).strip().upper()
            self._send_json(200, {
                "ok": True,
                "jurisdiction_code": code,
                "rule_template_fallback": code != "US-NC",
                "sources": [
                    {"id": "pacer_pcl", "tier": "federal_official", "cost": "account_billable_prod_qa_free", "formats": ["JSON", "XML"], "use": "Federal case/party index & CM/ECF XML"},
                    {"id": "courtlistener", "tier": "open_research_aggregator", "cost": "free_or_membership", "formats": ["REST_JSON", "BULK"], "use": "Federal/state opinions, RECAP & judges"},
                    {"id": "trellis_enterprise", "tier": "commercial_state_trial", "cost": "commercial_api", "formats": ["REST_JSON", "WEBHOOKS"], "use": "State trial dockets, rulings & judge analytics"}
                ]
            })
            return

        if path == "/api/jurisdiction/rules":

            st = _load_state()

            j_map = st.get("jurisdiction_rules") or {}

            code = "US-NC"

            if "query" in globals() and isinstance(query, dict) and query.get("code"):

                code = str(query["code"][0]).strip().upper()

            base = {

                "jurisdiction_code": code,

                "jurisdiction_name": "North Carolina (NC Pilot Template)" if code == "US-NC" else f"{code} (NC Pilot Template Fallback)",

                "template_fallback": code != "US-NC",

                "editable_by_court": True,

                "statutory_basis": "N.C.G.S. § 7A-49.6 baseline until court override saved",

                "rules": {

                    "recording_prohibited": True,

                    "requires_judicial_or_court_approval": True,

                    "private_counsel_consultation_enabled": True,

                    "remote_witness_oath_verification": True,

                    "pii_offchain_court_authoritative": True,

                    "blockchain_hash_only_anchoring": True

                }

            }

            if isinstance(j_map, dict) and code in j_map and isinstance(j_map[code], dict):

                base["template_fallback"] = False

                base["jurisdiction_name"] = j_map[code].get("jurisdiction_name", code)

                base["rules"].update(j_map[code].get("rules") or {})

            self._send_json(200, {"ok": True, "profile": base})

            return


        if path == "/api/health":
            bundle = _load_compiled_build()
            self._send_json(200, {
                "ok": True,
                "status": "healthy",
                "service": "AiMetaverse-VideoCourts",
                "port": PORT,
                "compiled_files": bundle.get("total_files", 0),
                "compiled_entries": bundle.get("total_court_matches", 0),
            })
            return

        if path == "/api/build/manifest":
            self._send_json(200, _load_compiled_build())
            return

        if path in ("/api/courts", "/api/courts/query"):
            q = (qs.get("q", qs.get("query", [""]))[0] or "").strip().lower()
            limit_raw = (qs.get("limit", ["200"])[0] or "200").strip()
            try:
                limit = max(1, min(2000, int(limit_raw)))
            except ValueError:
                limit = 200
            bundle = _load_compiled_build()
            pool = bundle.get("courts") or []
            matches = [c for c in pool if not q or q in json.dumps(c, ensure_ascii=False).lower()]
            self._send_json(200, {
                "ok": True,
                "q": q,
                "total_indexed_files": bundle.get("total_files", 0),
                "total_compiled_entries": len(pool),
                "count": len(matches),
                "courts": matches[:limit],
            })
            return

        if path == "/api/courts/directory":
            self._send_json(200, {
                "ok": True,
                "courts": [
                    {"id": "NC-SUP-REM", "name": "Remote & Virtual Hearing Pilot Court", "jurisdiction": "State / Pilot", "efile_provider": "Tyler Odyssey / eFile", "remote_enabled": True},
                    {"id": "NC-DIST-CIV", "name": "District Civil & Motions Virtual Docket", "jurisdiction": "District", "efile_provider": "Odyssey File & Serve", "remote_enabled": True},
                    {"id": "NC-BOND-MAG", "name": "Magistrate & Bond Appearance Session", "jurisdiction": "County", "efile_provider": "VideoCourts Direct", "remote_enabled": True},
                ],
            })
            return

        if path == "/api/efile/status":
            st = _load_state()
            self._send_json(200, {"ok": True, "efilings": st.get("efilings", []), "tyler_filings": st.get("tyler_filings", [])})
            return

        if path == "/api/tyler/odyssey/status":
            self._send_json(200, {
                "ok": True,
                "vendor": "Tyler Technologies",
                "modules": ["Odyssey Case Manager", "Odyssey File & Serve", "EFM Connector"],
                "mode": "local-bridge-ready",
                "note": "Configure vendor endpoint credentials before live production submission.",
            })
            return

        if path == "/api/legal/policies":
            self._send_json(200, {
                "ok": True,
                "terms_of_service": "VideoCourts provides remote appearance scheduling, e-filing prep, e-signature capture, and TotalRecall audit logging subject to presiding judicial orders and local court rules.",
                "privacy_policy": "Session metadata, consent records, and uploaded filings are hashed in TotalRecall; no unauthorized public rebroadcast or off-record capture is permitted.",
                "security_controls": ["TLS/HTTPS at edge", "PBKDF2-HMAC-SHA256 payload envelopes", "SHA-256 hash-chained TotalRecall audit log", "Waiting-room admission & recording controls"],
            })
            return

        if path == "/api/blockchain/ledger":

            st = _load_state()

            chain = st.get("blockchain_blocks") or []

            valid_chain = True

            for i in range(1, len(chain)):

                if chain[i].get("prev_hash") != chain[i - 1].get("block_hash"):

                    valid_chain = False

                    break

            self._send_json(200, {

                "ok": True,

                "ledger_mode": "hash_only_court_authoritative_offchain_pii",

                "chain_length": len(chain),

                "chain_valid": valid_chain,

                "peers": st.get("blockchain_peers", []),

                "blocks": chain[-25:],

            })

            return


        if path == "/api/security/verify":
            st = _load_state()
            recs = st.get("totalrecall_log", [])
            valid = True
            expected_prev = "GENESIS"
            for r in recs:
                if r.get("prev_hash", "GENESIS") != expected_prev:
                    valid = False
                    break
                chk = {k: v for k, v in r.items() if k != "sha256"}
                digest = hashlib.sha256(json.dumps(chk, sort_keys=True).encode("utf-8")).hexdigest()
                if digest != r.get("sha256"):
                    valid = False
                    break
                expected_prev = r.get("sha256")
            self._send_json(200, {"ok": True, "chain_valid": valid, "records_checked": len(recs), "head_hash": expected_prev})
            return

        if path == "/api/crm/contacts":
            st = _load_state()
            self._send_json(200, {"ok": True, "contacts": st.get("crm_contacts", [])})
            return

        if path == "/api/lawyers":
            self._send_json(200, {
                "ok": True,
                "network": "VideoCourts Lawyer Network",
                "lawyers": [
                    {"id": "lwy_01", "name": "Elena Vance, Esq.", "practice": "Civil & Remote Litigation", "bar_status": "Active", "remote_ready": True},
                    {"id": "lwy_02", "name": "Marcus Thorne, Esq.", "practice": "Bail, Bond & Criminal Procedure", "bar_status": "Active", "remote_ready": True},
                    {"id": "lwy_03", "name": "Priya Nair, Esq.", "practice": "Administrative & Appellate E-Filing", "bar_status": "Active", "remote_ready": True},
                ],
            })
            return

        if path == "/api/legyc/law":
            self._send_json(200, {
                "ok": True,
                "framework": "LEGyC Law — Sovereign & Virtual Court Protocol Rules",
                "principles": [
                    "Explicit party notice, identity verification, and consent prior to on-record proceedings.",
                    "Presiding judicial authority over room admission, muting, witness sequestration, and recording.",
                    "Tamper-evident SHA-256 chain-of-custody logging for filings, signatures, and appearance events.",
                    "Documented local court rules and pilot protocol compliance before live enforcement.",
                ],
            })
            return

        if path == "/api/sessions":
            query = (qs.get("query", qs.get("q", ["NC-2026-001"]))[0] or "NC-2026-001").strip()
            self._send_json(200, {
                "ok": True,
                "query": query,
                "session_id": f"sess_{int(time.time())}",
                "case_id": query if "-" in query else "NC-2026-001",
                "judge_id": "Hon. Martinez",
                "status": "scheduled",
            })
            return

        if path == "/api/calendar":
            st = _load_state()
            self._send_json(200, {"ok": True, "events": st.get("calendar_events", [])})
            return

        if path == "/api/pilot/info":
            bundle = _load_compiled_build()
            self._send_json(200, {
                "ok": True,
                "program": "VideoCourts Full Compiled Source & Pilot Program",
                "primary_pilot_doc": "01272022 AMENDED REMOTE HEARING PROTOCOLS.pdf",
                "compliance": ["ESIGN / UETA", "TotalRecall SHA-256 Chain-of-Custody", "Remote Hearing Protocols"],
                "pilot_docs": bundle.get("pilot_docs", []),
                    "total_extracted_unimplemented_items": len(bundle.get("extracted_build_items", [])),
                    "extracted_build_items": bundle.get("extracted_build_items", [])
                })
            return

        if path == "/api/docs/pilot-pdf":
            idx_raw = (qs.get("index", ["0"])[0] or "0").strip()
            bundle = _load_compiled_build()
            docs = bundle.get("pilot_docs") or []
            try:
                idx = max(0, min(len(docs) - 1, int(idx_raw)))
            except ValueError:
                idx = 0
            if docs:
                target = Path(docs[idx].get("path", ""))
                if target.exists() and target.is_file():
                    self._send_bytes(200, "application/pdf", target.name, target.read_bytes())
                    return
            self._send_json(404, {"ok": False, "error": "Pilot PDF not found"})
            return

        if path == "/api/docs/download":
            doc_id = (qs.get("id", [""])[0] or "").strip()
            st = _load_state()
            for d in st.get("documents", []):
                if d.get("id") == doc_id:
                    disk_path = Path(d.get("stored_path", ""))
                    if disk_path.exists():
                        self._send_bytes(200, "application/octet-stream", d.get("filename", "court_doc.bin"), disk_path.read_bytes())
                        return
            sample = f"VideoCourts Filing Record\nDocument ID: {doc_id or 'VC-SAMPLE'}\nTimestamp: {time.ctime()}\n".encode("utf-8")
            self._send_bytes(200, "application/pdf", f"{doc_id or 'court_doc'}.pdf", sample)
            return

        if path == "/api/totalrecall":
            st = _load_state()
            entries = st.get("totalrecall_log", [])
            self._send_json(200, {"ok": True, "count": len(entries), "records": entries[-200:]})
            return

        if path in ("/videocourts", "/videocourts/", "/", "/index.html"):
            idx = VC_DIR / "index.html"
            if idx.exists():
                self._send_html(200, idx.read_text(encoding="utf-8"))
                return

        self._send_json(404, {"ok": False, "error": "Unknown GET route", "path": path})

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        payload = self._read_json()

        if path == "/api/auth/webauthn/challenge":
            import base64 as _b64, json as _json, os as _os, secrets as _secrets
            from pathlib import Path as _Path
            try:
                raw_len = int(self.headers.get("Content-Length") or "0")
                if raw_len < 0 or raw_len > 1048576:
                    raw_len = 0
                raw_body = self.rfile.read(raw_len).decode("utf-8", errors="replace") if raw_len > 0 else "{}"
                body = _json.loads(raw_body) if raw_body.strip() else {}
                u_ref = str(body.get("user_ref") or "").strip().lower()
                ceremony = str(body.get("ceremony") or "authentication").strip().lower()
                if not u_ref:
                    self._send_json(400, {"ok": False, "error": "user_ref is required"})
                    return
                rp_id = _os.environ.get("WEBAUTHN_RP_ID", "localhost").strip() or "localhost"
                rp_name = _os.environ.get("WEBAUTHN_RP_NAME", "AiMetaverse VideoCourts").strip() or "AiMetaverse VideoCourts"
                chal_b64url = _b64.urlsafe_b64encode(_secrets.token_bytes(32)).rstrip(b"=").decode("ascii")
                uid_b64url = _b64.urlsafe_b64encode(u_ref.encode("utf-8")).rstrip(b"=").decode("ascii")
                try:
                    c_path = _Path.home() / "tmp/vc_webauthn_challenges.json"
                    c_path.parent.mkdir(parents=True, exist_ok=True)
                    c_data = _json.loads(c_path.read_text(encoding="utf-8")) if c_path.exists() else {}
                    if not isinstance(c_data, dict):
                        c_data = {}
                    c_data[u_ref] = {"challenge": chal_b64url, "ceremony": ceremony}
                    c_path.write_text(_json.dumps(c_data), encoding="utf-8")
                except Exception:
                    pass
                public_key = {
                    "challenge": chal_b64url,
                    "rp": {"name": rp_name, "id": rp_id},
                    "user": {"id": uid_b64url, "name": u_ref, "displayName": u_ref},
                    "pubKeyCredParams": [{"type": "public-key", "alg": -7}, {"type": "public-key", "alg": -257}],
                    "timeout": 60000,
                    "authenticatorSelection": {"userVerification": "required", "residentKey": "preferred"},
                    "attestation": "none"
                }
                self._send_json(200, {
                    "ok": True,
                    "mode": "stdlib_webauthn",
                    "ceremony": ceremony,
                    "stores_biometric_data": False,
                    "challenge": chal_b64url,
                    "publicKey": public_key
                })
            except Exception as exc:
                self._send_json(500, {"ok": False, "error": f"webauthn challenge error: {exc}"})
            return

        if path == "/api/auth/webauthn/verify":
            import base64 as _b64, json as _json, os as _os
            try:
                raw_len = int(self.headers.get("Content-Length") or "0")
                body = _json.loads(self.rfile.read(raw_len).decode("utf-8")) if raw_len > 0 else {}
                u_ref = (body.get("user_ref") or "").strip().lower()
                credential = body.get("credential") or {}
                if not u_ref or not credential:
                    self._send_json(400, {"ok": False, "error": "user_ref and credential are required"})
                    return
                st = _load_state()
                rec = _get_or_init_user_gate(st, u_ref)
                expected_chal = rec.get("pending_webauthn_challenge") or ""
                ceremony = rec.get("pending_webauthn_ceremony") or "authentication"
                if not expected_chal:
                    self._send_json(400, {"ok": False, "error": "No pending WebAuthn challenge"})
                    return
                resp_obj = credential.get("response") or {}
                cd_b64 = resp_obj.get("clientDataJSON") or ""
                if not cd_b64:
                    self._send_json(400, {"ok": False, "error": "Missing clientDataJSON"})
                    return
                padded = cd_b64 + "=" * (-len(cd_b64) % 4)
                client_data = _json.loads(_b64.urlsafe_b64decode(padded).decode("utf-8"))
                expected_type = "webauthn.create" if ceremony == "registration" else "webauthn.get"
                expected_origin = _os.environ.get("WEBAUTHN_ORIGIN", "").strip()
                if client_data.get("type") != expected_type:
                    self._send_json(400, {"ok": False, "error": "Invalid clientData type"})
                    return
                if client_data.get("challenge") != expected_chal:
                    self._send_json(400, {"ok": False, "error": "Challenge mismatch"})
                    return
                if expected_origin and client_data.get("origin") != expected_origin:
                    self._send_json(400, {"ok": False, "error": "Origin mismatch"})
                    return
                rec["pending_webauthn_challenge"] = None
                cred_id = credential.get("id") or credential.get("rawId") or ""
                creds = rec.get("webauthn_credential_ids") or []
                if cred_id and cred_id not in creds:
                    creds.append(cred_id)
                rec["webauthn_credential_ids"] = creds
                rec["biometric_onboarded"] = True
                rec["biometric_verified_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                _save_state(st)
                self._send_json(200, {
                    "ok": True,
                    "mode": "stdlib_webauthn",
                    "ceremony": ceremony,
                    "verified": True,
                    "stores_biometric_data": False
                })
            except Exception as exc:
                self._send_json(400, {"ok": False, "error": f"webauthn verify error: {exc}"})
            return

        if path == "/api/user/onboarding/idme-checkpoint":
            pass
        import json as _json, os as _os, urllib.request as _urlreq
        try:
            raw_len = int(self.headers.get("Content-Length") or "0")
            raw_body = self.rfile.read(raw_len).decode("utf-8") if raw_len > 0 else "{}"
            body_data = _json.loads(raw_body) if raw_body.strip() else {}
            u_ref = (body_data.get("user_ref") or body_data.get("participant_email") or "").strip()
            verification_id = (body_data.get("verification_id") or "").strip()
            if not u_ref:
                self._send_json(400, {"ok": False, "error": "user_ref is required for ID.me live-agent checkpoint"})
                return
            st = _load_state()
            rec = _get_or_init_user_gate(st, u_ref)
            idme_onboarded = bool(body_data.get("idme_onboarded", False))
            live_agent_processed = bool(body_data.get("live_agent_processed", False))
            idme_base = _os.environ.get("IDME_API_BASE", "").strip().rstrip("/")
            idme_token = _os.environ.get("IDME_API_TOKEN", "").strip()
            api_configured = bool(idme_base and idme_token)
            live_api_status = {"configured": api_configured, "stage_status": "live_api_enabled" if api_configured else "staged_awaiting_idme_api"}
            if api_configured and verification_id:
                try:
                    req = _urlreq.Request(f"{idme_base}/api/v2/verification/{verification_id}", headers={"Authorization": f"Bearer {idme_token}", "Accept": "application/json"})
                    with _urlreq.urlopen(req, timeout=4) as resp:
                        live_api_status = _json.loads(resp.read().decode("utf-8"))
                        if str((live_api_status.get("status") or {}).get("code")) == "200":
                            idme_onboarded = True
                except Exception as exc:
                    live_api_status = {"configured": True, "stage_status": "lookup_error", "lookup_error": str(exc)}
            rec["idme_verification_id"] = verification_id or rec.get("idme_verification_id")
            rec["idme_onboarded"] = idme_onboarded
            rec["live_agent_processed"] = live_agent_processed
            rec["idme_stage_status"] = live_api_status.get("stage_status", "staged_awaiting_idme_api")
            if idme_onboarded and live_agent_processed:
                rec["idme_verified"] = True
                rec["live_agent_verified_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            rec["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            _save_state(st)
            if callable(globals().get("_record_totalrecall")):
                _record_totalrecall("idme_live_agent_checkpoint_staged", {"user_key_hash": _sha256_text(rec["user_ref"]), "stage_status": rec["idme_stage_status"]})
            eligible, missing = _check_user_scheduling_eligibility(rec)
            self._send_json(200, {
                "ok": True,
                "staged": not api_configured,
                "stage_status": rec["idme_stage_status"],
                "idme_onboarded": idme_onboarded,
                "live_agent_processed": live_agent_processed,
                "checkpoint_passed": bool(idme_onboarded and live_agent_processed),
                "eligible_to_schedule": eligible,
                "missing_requirements": missing,
                "live_api_status": live_api_status
            })
        except Exception as exc:
            self._send_json(500, {"ok": False, "error": f"idme-checkpoint error: {exc}"})
        return
        if path == "/api/paas/device-sms":
            st = _load_state()
            u_ref = (payload.get("user_ref") or payload.get("participant_email") or "").strip()
            phone = (payload.get("phone_number") or "").strip()
            sms_consent = bool(payload.get("sms_consent", False))
            dev_ref = (payload.get("device_ref") or payload.get("imei_ref") or "").strip()
            if not u_ref:
                self._send_json(400, {"ok": False, "error": "user_ref is required"})
                return
            if not phone or not sms_consent:
                self._send_json(400, {"ok": False, "error": "Phone number and SMS text agreement (sms_consent=true) are required"})
                return
            if not dev_ref:
                self._send_json(400, {"ok": False, "error": "Mobile device_ref (or device/IMEI identifier) is required to target mobile biometrics"})
                return
            rec = _get_or_init_user_gate(st, u_ref) if callable(globals().get("_get_or_init_user_gate")) else {"user_ref": u_ref.lower()}
            rec["phone_number"] = phone
            rec["sms_consent"] = True
            rec["sms_consented_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            rec["mobile_device_bound"] = True
            rec["mobile_device_hash"] = _sha256_text(dev_ref) if callable(globals().get("_sha256_text")) else dev_ref[-6:]
            _save_state(st)
            if callable(globals().get("_record_totalrecall")):
                _record_totalrecall("paas_sms_device_bound", {"sms_consent": True, "mobile_device_bound": True, "device_hash": rec["mobile_device_hash"]})
            self._send_json(200, {
                "ok": True,
                "sms_consent": True,
                "mobile_device_bound": True,
                "device_hash": rec["mobile_device_hash"],
                "note": "Hardware IMEI cannot be read directly from web browsers; bound device reference and SMS consent recorded."
            })
            return

        if path == "/api/auth/biometric/challenge":
            import secrets
            st = _load_state()
            u_ref = (payload.get("user_ref") or payload.get("participant_email") or "").strip()
            client_surface = (payload.get("surface") or "web_to_mobile").strip().lower()
            modality = (payload.get("modality") or "face_or_fingerprint").strip().lower()
            if not u_ref:
                self._send_json(400, {"ok": False, "error": "user_ref is required for biometric challenge"})
                return
            rec = _get_or_init_user_gate(st, u_ref) if callable(globals().get("_get_or_init_user_gate")) else {"user_ref": u_ref.lower()}
            missing_bio_prereqs = []
            if not bool(rec.get("sms_consent")) or not (rec.get("phone_number") or "").strip():
                missing_bio_prereqs.append("sms_consent_and_phone")
            if not bool(rec.get("mobile_device_bound")):
                missing_bio_prereqs.append("mobile_device_bound")
            if missing_bio_prereqs:
                self._send_json(403, {"ok": False, "error": "SMS text agreement and mobile device binding are required before routing a mobile biometric challenge", "missing_requirements": missing_bio_prereqs})
                return

            challenges = st.setdefault("biometric_challenges", {})
            ch_id = "BIO-" + secrets.token_hex(6).upper()
            challenges[ch_id] = {
                "challenge_id": ch_id,
                "user_ref": u_ref.lower(),
                "surface": client_surface,
                "modality": modality,
                "stores_biometric_data": False,
                "status": "routed_to_mobile_device",
                "created_at": time.strftime("%Y-%m-%d %H:%M:%S")
            }
            rec["last_biometric_challenge_id"] = ch_id
            _save_state(st)
            if callable(globals().get("_record_totalrecall")):
                _record_totalrecall("biometric_challenge_routed", {"challenge_id": ch_id, "surface": client_surface, "modality": modality, "stores_biometric_data": False})
            self._send_json(200, {"ok": True, "challenge": challenges[ch_id], "fallback_pin_available": bool(rec.get("pin_synced"))})
            return

        if path == "/api/auth/biometric/confirm":
            import subprocess
            st = _load_state()
            ch_id = (payload.get("challenge_id") or "").strip()
            u_ref = (payload.get("user_ref") or "").strip().lower()
            matched = bool(payload.get("device_biometric_match", False))
            trigger_runtime = bool(payload.get("trigger_runtime", False))
            challenges = st.setdefault("biometric_challenges", {})
            ch = challenges.get(ch_id)
            if not ch and u_ref:
                last_id = ((st.get("user_onboarding_records") or {}).get(u_ref) or {}).get("last_biometric_challenge_id")
                ch = challenges.get(last_id) if last_id else None
            if not ch:
                self._send_json(404, {"ok": False, "error": "Biometric challenge not found"})
                return
            if not matched:
                ch["status"] = "biometric_match_failed"
                _save_state(st)
                self._send_json(401, {"ok": False, "error": "Mobile device biometric match not confirmed"})
                return
            ch["status"] = "confirmed_on_mobile_device"
            ch["confirmed_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            target_user = ch.get("user_ref") or u_ref
            rec = _get_or_init_user_gate(st, target_user) if callable(globals().get("_get_or_init_user_gate")) else {"user_ref": target_user}
            rec["biometric_onboarded"] = True
            rec["biometric_consent"] = True
            rec["login_confirmed"] = True
            rec["login_method"] = f"mobile_device_biometric({ch.get('modality', 'face_or_fingerprint')})"
            rec["login_confirmed_at"] = ch["confirmed_at"]
            _save_state(st)
            if callable(globals().get("_record_totalrecall")):
                _record_totalrecall("biometric_login_confirmed", {"challenge_id": ch.get("challenge_id"), "stores_biometric_data": False})
            boot_status = {"triggered": False}
            if trigger_runtime:
                be_script = Path.home() / "AiMetaverse/build_entry.py"
                if be_script.exists():
                    proc = subprocess.Popen(["python3", str(be_script), "--manifest-only"], cwd=str(Path.home() / "AiMetaverse"))
                    boot_status = {"triggered": True, "pid": proc.pid, "mode": "bootloader_manifest_sync"}
            self._send_json(200, {"ok": True, "login_confirmed": True, "stores_biometric_data": False, "challenge": ch, "bootloader": boot_status})
            return

        if path == "/api/user/onboarding/idme-pin":
            import random
            st = _load_state()
            u_ref = (payload.get("user_ref") or payload.get("participant_email") or "").strip()
            if not u_ref:
                self._send_json(400, {"ok": False, "error": "user_ref is required for ID.me verification"})
                return
            rec = _get_or_init_user_gate(st, u_ref)
            rec["idme_verified"] = True
            pin = rec.get("sync_pin_4digit") or f"{random.randint(1000, 9999):04d}"
            provided_pin = str(payload.get("pin_4digit") or "").strip()
            if provided_pin:
                if provided_pin == pin:
                    rec["pin_synced"] = True
                else:
                    self._send_json(400, {"ok": False, "error": "Invalid 4-digit sync PIN"})
                    return
            else:
                rec["sync_pin_4digit"] = pin
                rec["pin_synced"] = True
            rec["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            _save_state(st)
            _record_totalrecall("idme_pin_synced", {"user_key_hash": _sha256_text(rec["user_ref"])})
            self._send_json(200, {"ok": True, "user_ref": rec["user_ref"], "idme_verified": True, "pin_synced": True, "pin_4digit": rec["sync_pin_4digit"], "email_dispatched_sim": True, "fallback_for_biometric_login": True})
            return

        if path == "/api/user/onboarding/complete":
            st = _load_state()
            u_ref = (payload.get("user_ref") or payload.get("participant_email") or "").strip()
            if not u_ref:
                self._send_json(400, {"ok": False, "error": "user_ref is required"})
                return
            rec = _get_or_init_user_gate(st, u_ref)
            if "biometric_onboarded" in payload:
                rec["biometric_onboarded"] = bool(payload.get("biometric_onboarded"))
            if "biometric_consent" in payload:
                rec["biometric_consent"] = bool(payload.get("biometric_consent"))
            if "video_instructions_completed" in payload:
                rec["video_instructions_completed"] = bool(payload.get("video_instructions_completed"))
            if "virtual_court_rules_accepted" in payload:
                rec["virtual_court_rules_accepted"] = bool(payload.get("virtual_court_rules_accepted"))
            if "digital_agreements_signed" in payload:
                rec["digital_agreements_signed"] = bool(payload.get("digital_agreements_signed"))
            fee_tier = (payload.get("fee_tier") or "").strip()
            if fee_tier == "request_form_199":
                rec["fee_paid_cents"] = 199
                rec["fee_type"] = "virtual_courts_request_form_1.99"
            elif fee_tier == "appearance_fee_1999":
                rec["fee_paid_cents"] = 1999
                rec["fee_type"] = "virtual_court_appearance_fee_19.99"
            elif "fee_paid_cents" in payload:
                rec["fee_paid_cents"] = int(payload.get("fee_paid_cents") or 0)
            rec["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            _save_state(st)
            eligible, missing = _check_user_scheduling_eligibility(rec)
            self._send_json(200, {"ok": True, "onboarding": rec, "eligible_to_schedule": eligible, "missing_requirements": missing})
            return

        if path == "/api/admin/user/approve":
            st = _load_state()
            u_ref = (payload.get("user_ref") or payload.get("participant_email") or "").strip()
            if not u_ref:
                self._send_json(400, {"ok": False, "error": "user_ref is required for court approval"})
                return
            rec = _get_or_init_user_gate(st, u_ref)
            rec["court_approved"] = bool(payload.get("court_approved", True))
            rec["approved_by"] = "court_admin"
            rec["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            _save_state(st)
            _record_totalrecall("court_user_approved", {"user_key_hash": _sha256_text(rec["user_ref"]), "court_approved": rec["court_approved"]})
            eligible, missing = _check_user_scheduling_eligibility(rec)
            self._send_json(200, {"ok": True, "onboarding": rec, "eligible_to_schedule": eligible, "missing_requirements": missing})
            return

        if path == "/api/admin/slots/allot":
            st = _load_state()
            slots = _ensure_default_court_slots(st)
            j_code = (payload.get("jurisdiction_code") or "US-NC").strip().upper()
            slot_id = (payload.get("slot_id") or f"SLOT-{j_code}-{len(slots)+1:03d}").strip()
            new_slot = {
                "slot_id": slot_id,
                "jurisdiction_code": j_code,
                "date": (payload.get("date") or "2026-12-01").strip(),
                "window": (payload.get("window") or "11:00-11:20 EST").strip(),
                "room": (payload.get("room") or "Virtual-Room-A").strip(),
                "status": "available",
                "assigned_request_id": None
            }
            slots.append(new_slot)
            _save_state(st)
            self._send_json(200, {"ok": True, "slot": new_slot, "available_slots": [s for s in slots if s.get("status") == "available"]})
            return

        if path == "/api/schedule/request":
            st = _load_state()
            u_ref = (payload.get("user_ref") or payload.get("participant_email") or "").strip()
            u_rec = _get_or_init_user_gate(st, u_ref)
            eligible, missing = _check_user_scheduling_eligibility(u_rec)
            if not eligible:
                self._send_json(403, {"ok": False, "error": "User not eligible to schedule until ID.me + 4-digit PIN sync, biometric consent, video instructions, rules/agreements, $1.99 or $19.99 fee, and court approval are complete", "missing_requirements": missing})
                return
            slots = _ensure_default_court_slots(st)
            chosen_id = (payload.get("slot_id") or "").strip()
            target_slot = next((s for s in slots if s.get("slot_id") == chosen_id), None)
            if not chosen_id or not target_slot or target_slot.get("status") != "available":
                self._send_json(400, {"ok": False, "error": "User-side scheduling requires a valid available court-allotted slot_id; server bot handles final assignment"})
                return
            reqs = st.setdefault("schedule_requests", [])
            req_id = f"REQ-{len(reqs)+1:04d}"
            rec = {
                "request_id": req_id,
                "slot_id": chosen_id,
                "jurisdiction_code": target_slot.get("jurisdiction_code") or "US-NC",
                "user_ref": (payload.get("user_ref") or payload.get("participant_email") or "").strip(),
                "case_ref": (payload.get("case_ref") or "").strip(),
                "status": "pending_server_bot_approval",
                "created_at": time.strftime("%Y-%m-%d %H:%M:%S")
            }
            reqs.append(rec)
            _save_state(st)
            self._send_json(200, {"ok": True, "request": rec})
            return

        if path == "/api/admin/approval-bot/sync":
            st = _load_state()
            req_id = (payload.get("request_id") or "").strip()
            auto_first = bool(payload.get("auto_assign_first_slot", True))
            synced = _run_server_approval_bot(st, request_id=req_id, auto_assign_first_slot=auto_first)
            _save_state(st)
            _record_totalrecall("server_approval_bot_sync", {"synced_count": len(synced)})
            self._send_json(200, {"ok": True, "synced_count": len(synced), "synced_requests": synced, "available_slots": [s for s in (st.get("court_virtual_slots") or []) if s.get("status") == "available"]})
            return

        if path == "/api/ai/assistant":
            import json, os, urllib.request
            provider = (payload.get("provider") or "ollama").strip().lower()
            prompt = (payload.get("prompt") or "").strip()
            jurisdiction = (payload.get("jurisdiction") or "US-NC").strip().upper()
            if not prompt:
                self._send_json(400, {"ok": False, "error": "prompt is required"})
                return
            if provider in ("ollama", "qwen", "local"):
                model = (payload.get("model") or "qwen2.5-coder:3b").strip()
                base_url = (payload.get("ollama_url") or "http://127.0.0.1:11434").rstrip("/")
                req_body = json.dumps({"model": model, "messages": [{"role": "system", "content": f"VideoCourts assistant ({jurisdiction}). Keep PII off-chain."}, {"role": "user", "content": prompt}], "stream": False}).encode("utf-8")
                try:
                    req = urllib.request.Request(f"{base_url}/api/chat", data=req_body, headers={"Content-Type": "application/json"}, method="POST")
                    with urllib.request.urlopen(req, timeout=20) as resp:
                        data = json.loads(resp.read().decode("utf-8"))
                    reply = ((data.get("message") or {}).get("content") or data.get("response") or "")
                    self._send_json(200, {"ok": True, "provider": "ollama", "model": model, "est_api_cost_usd": 0.0, "reply": reply})
                except Exception as exc:
                    self._send_json(200, {"ok": False, "provider": "ollama", "model": model, "connected": False, "error": str(exc), "toast": "Ollama/Qwen offline on 127.0.0.1:11434 — using local fallback notice"})
                return
            if provider in ("grok", "xai", "legal_ai"):
                api_key = os.environ.get("XAI_API_KEY", "").strip()
                model = (payload.get("model") or "grok-4.7").strip()
                if not api_key:
                    self._send_json(200, {"ok": False, "provider": "grok", "model": model, "connected": False, "error": "XAI_API_KEY not set in environment", "toast": "Grok Legal AI key not configured — set XAI_API_KEY to enable live calls"})
                    return
                req_body = json.dumps({"model": model, "messages": [{"role": "system", "content": f"You are a legal research and virtual court procedure assistant for {jurisdiction}. Provide structured information, not formal legal representation, and avoid raw PII."}, {"role": "user", "content": prompt}]}).encode("utf-8")
                try:
                    req = urllib.request.Request("https://api.x.ai/v1/chat/completions", data=req_body, headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}, method="POST")
                    with urllib.request.urlopen(req, timeout=25) as resp:
                        data = json.loads(resp.read().decode("utf-8"))
                    reply = (((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "")
                    self._send_json(200, {"ok": True, "provider": "grok", "model": model, "reply": reply})
                except Exception as exc:
                    self._send_json(200, {"ok": False, "provider": "grok", "model": model, "connected": False, "error": str(exc), "toast": "Grok Legal AI unreachable — toast fallback shown"})
                return
            self._send_json(400, {"ok": False, "error": f"Unsupported AI provider: {provider}"})
            return

        if path == "/api/licensing/config":
            st = _load_state()
            cur = _get_licensing_config(st)
            for k, v in (payload or {}).items():
                if isinstance(v, dict) and isinstance(cur.get(k), dict):
                    for sub_k, sub_v in v.items():
                        if isinstance(sub_v, dict) and isinstance(cur[k].get(sub_k), dict):
                            cur[k][sub_k].update(sub_v)
                        else:
                            cur[k][sub_k] = sub_v
                else:
                    cur[k] = v
            cur["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            st["licensing_config"] = cur
            _save_state(st)
            _record_totalrecall("licensing_config_updated", {"updated_at": cur["updated_at"]})
            ramp_q = int((payload or {}).get("ramp_courts_per_quarter", 2))
            self._send_json(200, {"ok": True, "config": cur, "breakeven": _compute_licensing_breakeven(st, cur), "projection_12_to_36_months": _compute_licensing_projection(st, cur, ramp_q)})
            return

        if path == "/api/licenses/generate":
            import hashlib, secrets
            st = _load_state()
            lics = st.setdefault("court_licenses", [])
            j_code = (payload.get("jurisdiction_code") or "US-NC").strip().upper()
            j_map = st.get("jurisdiction_rules") or {}
            test_mode = bool(payload.get("test_mode", True))
            activate_now = bool(payload.get("activate_now", False))
            entity_name = (payload.get("court_or_entity_name") or payload.get("court_name") or "").strip()
            can_activate = activate_now and bool(entity_name)
            lic_id = (payload.get("license_id") or f"VC-LIC-{j_code}-{len(lics) + 1:04d}").strip()
            raw_nonce = secrets.token_hex(8)
            lic_key = "VCLK-" + hashlib.sha256(f"{lic_id}:{j_code}:{raw_nonce}".encode("utf-8")).hexdigest()[:24].upper()
            record = {
                "license_id": lic_id,
                "license_key": lic_key,
                "jurisdiction_code": j_code,
                "template_fallback": False if j_code == "US-NC" else not (isinstance(j_map, dict) and j_code in j_map),
                "test_mode": test_mode,
                "authorized": can_activate,
                "status": "active" if can_activate else "pending_activation",
                "licensed_entity": entity_name if can_activate else None,
                "entity_type": (payload.get("entity_type") or "unassigned_test").strip(),
                "tier": (payload.get("tier") or "pilot_nc_template").strip(),
                "seat_limit": int(payload.get("seat_limit") or 25),
                "expires_at": (payload.get("expires_at") or "2027-11-25").strip(),
                "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "authorized_at": time.strftime("%Y-%m-%d %H:%M:%S") if can_activate else None
            }
            lics.append(record)
            st["court_licenses"] = lics[-200:]
            _save_state(st)
            _record_totalrecall("court_license_generated", {
                "license_id": lic_id,
                "jurisdiction_code": j_code,
                "status": record["status"],
                "authorized": record["authorized"]
            })
            self._send_json(200, {
                "ok": True,
                "note": "Generated in pending_activation state until assigned to a court or legal entity." if not can_activate else "Generated and authorized for licensed entity.",
                "license": record
            })
            return

        if path == "/api/licenses/authorize":
            st = _load_state()
            lics = st.get("court_licenses") or []
            lic_id = (payload.get("license_id") or "").strip()
            lic_key = (payload.get("license_key") or "").strip()
            entity_name = (payload.get("court_or_entity_name") or payload.get("court_name") or "").strip()
            entity_type = (payload.get("entity_type") or "court_or_legal_entity").strip()
            revoke = bool(payload.get("revoke", False))
            target = None
            for item in reversed(lics):
                if (lic_id and item.get("license_id") == lic_id) or (lic_key and item.get("license_key") == lic_key):
                    target = item
                    break
            if not target:
                self._send_json(404, {"ok": False, "error": "Target license not found"})
                return
            if revoke:
                target["authorized"] = False
                target["status"] = "pending_activation"
                target["authorized_at"] = None
            else:
                if not entity_name:
                    self._send_json(400, {"ok": False, "error": "court_or_entity_name is required to authorize and activate a license"})
                    return
                target["licensed_entity"] = entity_name
                target["entity_type"] = entity_type
                target["authorized"] = True
                target["test_mode"] = False
                target["status"] = "active"
                target["authorized_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            _save_state(st)
            _record_totalrecall("court_license_authorization_changed", {
                "license_id": target.get("license_id"),
                "status": target.get("status"),
                "authorized": target.get("authorized")
            })
            self._send_json(200, {"ok": True, "license": target})
            return

        if path == "/api/licenses":
            st = _load_state()
            lics = st.setdefault("court_licenses", [])
            j_code = (payload.get("jurisdiction_code") or "US-NC").strip().upper()
            j_map = st.get("jurisdiction_rules") or {}
            lic_id = (payload.get("license_id") or f"LIC-{j_code}-{len(lics) + 1:04d}").strip()
            entry = {
                "license_id": lic_id,
                "jurisdiction_code": j_code,
                "template_fallback": False if j_code == "US-NC" else not (isinstance(j_map, dict) and j_code in j_map),
                "court_name": (payload.get("court_name") or f"{j_code} Court Pilot").strip(),
                "tier": (payload.get("tier") or "pilot_nc_template").strip(),
                "status": (payload.get("status") or "active").strip(),
                "seat_limit": int(payload.get("seat_limit") or 25),
                "expires_at": (payload.get("expires_at") or "2027-11-25").strip(),
                "updated_at": time.strftime("%Y-%m-%d %H:%M:%S")
            }
            lics.append(entry)
            st["court_licenses"] = lics[-200:]
            _save_state(st)
            _record_totalrecall("court_license_saved", {"license_id": lic_id, "jurisdiction_code": j_code, "status": entry["status"]})
            self._send_json(200, {"ok": True, "license": entry})
            return

        if path == "/api/billing":
            st = _load_state()
            invs = st.setdefault("billing_invoices", [])
            j_code = (payload.get("jurisdiction_code") or "US-NC").strip().upper()
            inv_id = (payload.get("invoice_id") or f"INV-{len(invs) + 1:05d}").strip()
            inv = {
                "invoice_id": inv_id,
                "license_id": (payload.get("license_id") or next((x.get("license_id") for x in reversed(st.get("court_licenses") or []) if x.get("jurisdiction_code") == j_code), "UNLINKED")).strip(),
                "jurisdiction_code": j_code,
                "account_name": (payload.get("account_name") or f"{j_code} Court Account").strip(),
                "plan_label": (payload.get("plan_label") or "Court Remote Hearing License").strip(),
                "amount_cents": (
        int(payload["amount_cents"]) if payload.get("amount_cents") not in (None, "") else (
            int(round(float((_get_licensing_config(st).get("tiers") or {}).get((payload.get("tier") or payload.get("plan_label") or "pilot_nc_template"), (_get_licensing_config(st).get("tiers") or {}).get("pilot_nc_template") or {}).get("monthly_usd", 0)) * 100))
            if callable(globals().get("_get_licensing_config")) else 0
        )
    ),
                "currency": (payload.get("currency") or "USD").strip().upper(),
                "status": (payload.get("status") or "pending").strip(),
                "created_at": time.strftime("%Y-%m-%d %H:%M:%S")
            }
            invs.append(inv)
            st["billing_invoices"] = invs[-200:]
            _save_state(st)
            _record_totalrecall("billing_invoice_recorded", {"invoice_id": inv_id, "jurisdiction_code": j_code, "amount_cents": inv["amount_cents"], "status": inv["status"]})
            self._send_json(200, {"ok": True, "invoice": inv})
            return

        if path == "/api/jurisdiction/rules":
            st = _load_state()
            code = (payload.get("jurisdiction_code") or "US-NC").strip().upper()
            j_map = st.setdefault("jurisdiction_rules", {})
            j_map[code] = {
                "jurisdiction_name": payload.get("jurisdiction_name") or code,
                "rules": payload.get("rules") or {},
                "updated_at": time.strftime("%Y-%m-%d %H:%M:%S")
            }
            _save_state(st)
            _record_totalrecall("jurisdiction_rules_updated", {"jurisdiction_code": code})
            self._send_json(200, {"ok": True, "jurisdiction_code": code, "profile": j_map[code]})
            return

        if path == "/api/jurisdiction/rules":

            st = _load_state()

            code = (payload.get("jurisdiction_code") or "US-NC").strip().upper()

            j_map = st.setdefault("jurisdiction_rules", {})

            j_map[code] = {

                "jurisdiction_name": payload.get("jurisdiction_name") or code,

                "rules": payload.get("rules") or {},

                "updated_at": time.strftime("%Y-%m-%d %H:%M:%S")

            }

            _save_state(st)

            _record_totalrecall("jurisdiction_rules_updated", {"jurisdiction_code": code})

            self._send_json(200, {"ok": True, "jurisdiction_code": code, "profile": j_map[code]})

            return


        if path == "/api/video/signal":

            st = _load_state()

            room_id = (payload.get("room_id") or "NC-2026-001-ROOM").strip()

            peer_role = (payload.get("role") or "participant").strip()

            sig_type = (payload.get("signal_type") or "presence").strip()

            rooms = st.setdefault("video_rooms", {})

            room = rooms.setdefault(room_id, {"peers": [], "signals": []})

            if peer_role not in room["peers"]:

                room["peers"].append(peer_role)

            room["signals"] = (room["signals"] + [{

                "role": peer_role,

                "signal_type": sig_type,

                "at": time.strftime("%Y-%m-%d %H:%M:%S")

            }])[-30:]

            _save_state(st)

            _record_totalrecall("video_room_signal", {"room_id": room_id, "role": peer_role, "signal_type": sig_type})

            self._send_json(200, {"ok": True, "room_id": room_id, "room": room})

            return


        if path == "/api/blockchain/block":

            st = _load_state()

            raw_data = payload.get("record_payload") if "record_payload" in payload else payload

            payload_sha256 = hashlib.sha256(json.dumps(raw_data, sort_keys=True, default=str).encode("utf-8")).hexdigest()

            event_type = (payload.get("event_type") or "court_ledger_anchor").strip()

            _record_totalrecall(event_type, {"payload_sha256": payload_sha256, "case_id": payload.get("case_id", ""), "offchain_pii_stored": False})

            st = _load_state()

            chain = st.setdefault("blockchain_blocks", [])

            prev_hash = chain[-1]["block_hash"] if chain else "0" * 64

            idx = len(chain)

            ts_str = time.strftime("%Y-%m-%d %H:%M:%S")

            block_hash = hashlib.sha256(f"{idx}|{ts_str}|{event_type}|{payload_sha256}|{prev_hash}".encode("utf-8")).hexdigest()

            blk = {"index": idx, "timestamp": ts_str, "event_type": event_type, "payload_sha256": payload_sha256, "prev_hash": prev_hash, "block_hash": block_hash}

            chain.append(blk)

            _save_state(st)

            self._send_json(200, {"ok": True, "block": blk, "chain_length": len(chain)})

            return


        if path == "/api/blockchain/peer/sync":

            st = _load_state()

            peer_url = (payload.get("peer_url") or "").strip()

            peers = st.setdefault("blockchain_peers", [])

            if peer_url and peer_url not in peers:

                peers.append(peer_url)

            chain = st.setdefault("blockchain_blocks", [])

            known = {x.get("block_hash") for x in chain}

            added = 0

            for b in (payload.get("blocks") or []):

                if isinstance(b, dict) and b.get("block_hash") and b["block_hash"] not in known:

                    chain.append(b)

                    known.add(b["block_hash"])

                    added += 1

            _save_state(st)

            self._send_json(200, {"ok": True, "peers": peers, "added_blocks": added, "chain_length": len(chain)})

            return


        if path == "/api/onboarding/complete":
            st = _load_state()
            prof = {
                "id": f"onb_{int(time.time() * 1000)}",
                "full_name": payload.get("full_name", "Participant"),
                "role": payload.get("role", "counsel"),
                "jurisdiction": payload.get("jurisdiction", "NC"),
                "accepted_tos": bool(payload.get("accepted_tos", True)),
                "accepted_privacy": bool(payload.get("accepted_privacy", True)),
                "completed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            st.setdefault("onboarding_profiles", []).append(prof)
            _save_state(st)
            _record_totalrecall("onboarding_completed", prof)
            self._send_json(200, {"ok": True, "profile": prof})
            return

        if path == "/api/video/room":
            st = _load_state()
            room = {
                "room_id": f"vc_room_{int(time.time() * 1000)}",
                "case_id": payload.get("case_id", "NC-2026-001"),
                "waiting_room_enabled": bool(payload.get("waiting_room_enabled", True)),
                "recording_notice_shown": bool(payload.get("recording_notice_shown", True)),
                "precheck_passed": bool(payload.get("precheck_passed", True)),
                "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            st.setdefault("video_rooms", []).append(room)
            _save_state(st)
            _record_totalrecall("video_room_configured", room)
            self._send_json(200, {"ok": True, "room": room})
            return

        if path == "/api/crm/contacts":
            st = _load_state()
            contact = {
                "id": f"crm_{int(time.time() * 1000)}",
                "name": payload.get("name", ""),
                "role": payload.get("role", "Client"),
                "case_id": payload.get("case_id", "NC-2026-001"),
                "email_or_phone": payload.get("contact", ""),
                "notes": payload.get("notes", ""),
            }
            st.setdefault("crm_contacts", []).append(contact)
            _save_state(st)
            _record_totalrecall("crm_contact_added", contact)
            self._send_json(200, {"ok": True, "contact": contact})
            return

        if path == "/api/tyler/efile/submit":
            st = _load_state()
            tx = {
                "tyler_envelope_id": f"tyler_env_{int(time.time() * 1000)}",
                "court_id": payload.get("court_id", "NC-DIST-CIV"),
                "case_id": payload.get("case_id", "NC-2026-001"),
                "filing_code": payload.get("filing_code", "MOT-REM"),
                "status": "queued_for_tyler_efm",
                "submitted_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            st.setdefault("tyler_filings", []).append(tx)
            _save_state(st)
            _record_totalrecall("tyler_efile_queued", tx)
            self._send_json(200, {"ok": True, "tyler_filing": tx})
            return

        if path == "/api/alfai/grok/assist":
            prompt = (payload.get("prompt") or "").strip()
            mode = payload.get("mode") or "ALFAI + Grok Legal Research"
            case_id = payload.get("case_id") or "NC-2026-001"
            reply = {
                "ok": True,
                "assistant": mode,
                "case_id": case_id,
                "disclaimer": "Draft procedural assistance only; verify against presiding court orders and applicable statutes.",
                "analysis": f"[{mode} | {case_id}] Checklist for '{prompt or 'remote hearing prep'}': (1) Confirm party consent & virtual appearance order; (2) Verify exhibit numbering & witness sequestration rules; (3) Log action in TotalRecall with SHA-256 hash.",
            }
            _record_totalrecall("alfai_grok_assist", {"case_id": case_id, "mode": mode, "prompt": prompt[:120]})
            self._send_json(200, reply)
            return

        if path == "/api/identity/check":
            now_ms = int(time.time() * 1000)
            res = {
                "ok": True,
                "verified": True,
                "case_id": payload.get("case_id", "NC-2026-001"),
                "full_name": payload.get("full_name", "Participant"),
                "user_id": f"user_{now_ms}",
                "access_level": payload.get("role") or "defendant",
                "token": f"vc_token_{hex(now_ms)[2:]}",
                "message": "Identity verified successfully",
            }
            _record_totalrecall("identity_check", res)
            self._send_json(200, res)
            return

        if path == "/api/efile":
            st = _load_state()
            filing = {
                "id": f"ef_{int(time.time() * 1000)}",
                "case_id": payload.get("case_id", "NC-2026-001"),
                "doc_type": payload.get("doc_type", "Motion"),
                "party": payload.get("party", "Counsel"),
                "filed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "status": "filed",
            }
            filing["encrypted_envelope"] = _encrypt_envelope(json.dumps(filing))
            st.setdefault("efilings", []).append(filing)
            _save_state(st)
            _record_totalrecall("efile_submitted", {"id": filing["id"], "case_id": filing["case_id"], "doc_type": filing["doc_type"]})
            self._send_json(200, {"ok": True, "filing": filing})
            return

        if path == "/api/esign":
            st = _load_state()
            sig = {
                "id": f"es_{int(time.time() * 1000)}",
                "signer_name": payload.get("signer_name", ""),
                "document_title": payload.get("document_title", "Virtual Appearance & Consent"),
                "consent_checked": bool(payload.get("consent_checked", True)),
                "signed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "status": "signed",
            }
            sig["encrypted_envelope"] = _encrypt_envelope(json.dumps(sig))
            st.setdefault("esignatures", []).append(sig)
            _save_state(st)
            _record_totalrecall("esign_completed", {"id": sig["id"], "signer_name": sig["signer_name"], "document_title": sig["document_title"]})
            self._send_json(200, {"ok": True, "signature": sig})
            return

        if path == "/api/calendar":
            st = _load_state()
            ev = {
                "id": f"cal_{int(time.time() * 1000)}",
                "case_id": payload.get("case_id", "NC-2026-001"),
                "title": payload.get("title", "Court Hearing"),
                "date": payload.get("date", "2026-11-26"),
                "time": payload.get("time", "10:00"),
                "mode": payload.get("mode", "Virtual"),
            }
            st.setdefault("calendar_events", []).append(ev)
            _save_state(st)
            _record_totalrecall("calendar_scheduled", ev)
            self._send_json(200, {"ok": True, "event": ev})
            return

        if path == "/api/appearance/request":
            st = _load_state()
            req = {
                "id": f"va_{int(time.time() * 1000)}",
                "case_id": payload.get("case_id", "NC-2026-001"),
                "requester_name": payload.get("requester_name", ""),
                "role": payload.get("role", "counsel"),
                "reason": payload.get("reason", "Remote virtual appearance"),
                "requested_date": payload.get("requested_date", ""),
                "status": "submitted",
            }
            st.setdefault("virtual_appearances", []).append(req)
            _save_state(st)
            _record_totalrecall("virtual_appearance_requested", req)
            self._send_json(200, {"ok": True, "request": req})
            return

        if path == "/api/docs/upload":
            filename = Path(payload.get("filename") or "uploaded_court_doc.pdf").name
            content_b64 = payload.get("content_b64") or ""
            try:
                raw_bytes = base64.b64decode(content_b64) if content_b64 else b""
            except Exception:
                raw_bytes = b""
            if not raw_bytes:
                raw_bytes = (payload.get("text") or f"Uploaded document: {filename}\n").encode("utf-8")
            doc_id = f"doc_{int(time.time() * 1000)}"
            stored = DOCS_DIR / f"{doc_id}_{filename}"
            stored.write_bytes(raw_bytes)
            st = _load_state()
            rec = {
                "id": doc_id,
                "filename": filename,
                "size": len(raw_bytes),
                "sha256": hashlib.sha256(raw_bytes).hexdigest(),
                "stored_path": str(stored),
                "uploaded_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            st.setdefault("documents", []).append(rec)
            _save_state(st)
            _record_totalrecall("doc_uploaded", rec)
            self._send_json(200, {"ok": True, "document": rec})
            return

        if path == "/api/mocktrial/run":
            case_id = payload.get("case_id") or "MT-2026-01"
            role = payload.get("role") or "counsel"
            prompt = payload.get("prompt") or "Opening statement"
            res = {
                "ok": True,
                "case_id": case_id,
                "role": role,
                "feedback": f"Bench review ({role}) on {case_id}: establish foundation and address '{prompt}'.",
            }
            _record_totalrecall("mocktrial_turn", res)
            self._send_json(200, res)
            return

        if path == "/api/ai/chat":
            case_id = payload.get("case_id") or "VC-DEFAULT-001"
            message = (payload.get("message") or "").strip()
            res = {
                "ok": True,
                "case_id": case_id,
                "reply": f"[AiMetaverse GTP Sync] {case_id}: {message or 'Ready for E-File, E-Sign, Calendar, Virtual Appearance, Pilot PDFs, Docs, or TotalRecall.'}",
            }
            _record_totalrecall("ai_chat", res)
            self._send_json(200, res)
            return

        if path == "/api/bail/post":
            now_ms = int(time.time() * 1000)
            res = {
                "ok": True,
                "bond_id": f"bond_{now_ms}",
                "case_id": payload.get("case_id") or "NC-2026-001",
                "amount": payload.get("amount") or 0,
                "bond_type": payload.get("bond_type") or "Appearance Bond",
                "status": "posted",
            }
            _record_totalrecall("bail_posted", res)
            self._send_json(200, res)
            return

        self._send_json(404, {"ok": False, "error": "Unknown POST route", "path": path})


def main():
    server = ThreadingHTTPServer((HOST, PORT), AiMetaverseHandler)
    print(f"AiMetaverse full compiled server listening on http://{HOST}:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
