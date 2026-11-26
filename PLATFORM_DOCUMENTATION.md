# VideoCourts Build Entry & Compliance Documentation

**Build Entry:** `python3 build_entry.py` | **Version:** `2026.11-build-entry` | **Generated:** `2026-11-25 20:03:29 EST`

## 1. PaaS & SaaS Security Controls
- **transport_encryption:** TLS 1.2+ required in transit (HTTPS/WSS reverse proxy in production)
- **storage_encryption:** Encryption at rest required for sensitive/regulated data stores
- **identity_and_access:** MFA/SSO support, least-privilege API keys, quarterly privilege review, no raw biometric templates
- **audit_and_forensics:** Tamper-evident SHA-256 hash chain + exportable security/access logs
- **data_lifecycle:** Court-authoritative off-chain custody for PII/sealed records; defined retention and purge/export controls

## 2. State & Judicial Standards Alignment
- **court_custody_and_sealing:** Courts retain authoritative case/PII records; redaction and sealed-filing rules respected
- **interoperability_standards:** Align schemas and exchanges with court/justice standards (NIEM, OASIS ECF, state e-filing rules)

## 3. Technology Vendor & Data Security Requirements
- **independent_assurance:** Current SOC 2 Type II / ISO 27001 / annual pen-test attestation tracking for sub-vendors
- **no_unauthorized_ai_training:** Strict prohibition on training external/commercial models on confidential legal or user data
- **incident_response_sla:** Rapid breach notification commitment (24-72h target escalation) and subprocessor flow-down terms
- **data_residency_and_dsp:** Approved jurisdictional residency and screening for covered/restricted data transactions (DOJ DSP / CISA security rules)

## 4. Artifacts & Endpoints
- **GET routes (29):** /api/ai/functions, /api/billing, /api/blockchain/ledger, /api/build/manifest, /api/calendar, /api/courts/directory, /api/courts/max-calendar, /api/crm/contacts, /api/dockets/track, /api/docs/download, /api/docs/pilot-pdf, /api/efile/status, /api/health, /api/jurisdiction/rules, /api/jurisdiction/sources, /api/lawyers, /api/legal/policies, /api/legyc/law, /api/licenses, /api/licenses/authorize, /api/licensing/config, /api/paas/profile, /api/pilot/info, /api/schedule/slots, /api/security/verify, /api/sessions, /api/totalrecall, /api/tyler/odyssey/status, /videocourts
- **POST routes (36):** /api/admin/approval-bot/sync, /api/admin/slots/allot, /api/admin/user/approve, /api/ai/assistant, /api/ai/chat, /api/alfai/grok/assist, /api/appearance/request, /api/auth/biometric/challenge, /api/auth/biometric/confirm, /api/auth/webauthn/challenge, /api/auth/webauthn/verify, /api/bail/post, /api/billing, /api/blockchain/block, /api/blockchain/peer/sync, /api/calendar, /api/crm/contacts, /api/docs/upload, /api/efile, /api/esign, /api/identity/check, /api/jurisdiction/rules, /api/licenses, /api/licenses/authorize, /api/licenses/generate, /api/licensing/config, /api/mocktrial/run, /api/onboarding/complete, /api/paas/device-sms, /api/schedule/request, /api/tyler/efile/submit, /api/user/onboarding/complete, /api/user/onboarding/idme-checkpoint, /api/user/onboarding/idme-pin, /api/video/room, /api/video/signal
- **UI modules (18):** efileSec, esignSec, calSec, appearSec, pilotSec, docsSec, courtsSec, videoRoomSec, onboardingSec, jurisdictionRulesSec, docketTrackerSec, licensingBillingSec, trSec, paasSmsDeviceSec, mobileBiometricBridgeSec, idmeLiveCheckpointSec, userOnboardingGateSec, serverApprovalBotSec
