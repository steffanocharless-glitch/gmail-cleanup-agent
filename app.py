"""AI Gmail Cleanup Agent - Streamlit entry point.

Each user authenticates their own Gmail account via Composio OAuth. Nothing
here uses a shared/pre-existing connection - every session starts unconnected.
"""
from __future__ import annotations

from collections import Counter
from datetime import date, timedelta

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

from src.cleanup_engine import execute_cleanup, scan_and_classify
from src.classifier import EmailClassifier
from src.composio_service import ComposioService, ComposioServiceError
from src.config import Action, Category, get_config
from src.gmail_service import GmailService
from src.identity import derive_user_id
from src.logger import AuditLogger, get_logger

logger = get_logger(__name__)

st.set_page_config(page_title="AI Gmail Cleanup Agent", page_icon="📬", layout="wide")

st.markdown(
    """
    <style>
    #MainMenu, footer, header {visibility: hidden;}
    .block-container {padding-top: 2rem; padding-bottom: 2rem; max-width: 1100px;}
    div[data-testid="stCaptionContainer"] {opacity: 0.65;}
    hr {margin: 0.5rem 0; border-color: rgba(0,229,255,0.25);}

    /* Layer 1: base - dimensional dark charcoal, not a flat fill. */
    div[data-testid="stAppViewContainer"] {
        background-color: #06070d;
        background-image:
            linear-gradient(165deg, #0a0d18 0%, #05060c 55%, #070911 100%),
            repeating-linear-gradient(0deg, rgba(0,229,255,0.035) 0px, rgba(0,229,255,0.035) 1px, transparent 1px, transparent 48px),
            repeating-linear-gradient(90deg, rgba(0,229,255,0.035) 0px, rgba(0,229,255,0.035) 1px, transparent 1px, transparent 48px);
        background-attachment: fixed;
        position: relative;
    }
    /* Layer 2: ambient light zones + the existing cursor glow (unchanged
       --mx/--my logic - listed first so it renders above the ambient
       orbs and stays the dominant light source). Restrained cool tones,
       low opacity, slow opacity-only breathing (no position drift, so it
       can never fight the cursor glow's own positioning). */
    div[data-testid="stAppViewContainer"]::before {
        content: "";
        position: fixed;
        inset: 0;
        z-index: 0;
        pointer-events: none;
        background:
            radial-gradient(circle at var(--mx, 50%) var(--my, 30%), rgba(0,229,255,0.16), transparent 26%),
            radial-gradient(ellipse 60% 40% at 20% 15%, rgba(0,229,255,0.10), transparent 55%),
            radial-gradient(ellipse 55% 45% at 82% 78%, rgba(139,92,246,0.09), transparent 55%),
            radial-gradient(ellipse 50% 35% at 55% 95%, rgba(79,70,229,0.07), transparent 60%);
        animation: glow-breathe 14s ease-in-out infinite alternate;
    }
    @keyframes glow-breathe {
        from { opacity: 0.75; }
        to { opacity: 1; }
    }
    /* Layer 3: soft edge vignette for depth - static, sits above the glow
       layer, still below actual content (z-index: 1 below). */
    div[data-testid="stAppViewContainer"]::after {
        content: "";
        position: fixed;
        inset: 0;
        z-index: 0;
        pointer-events: none;
        box-shadow: inset 0 0 220px 70px rgba(0,0,0,0.5);
    }
    /* Layer 4: actual page content, above every decorative layer. */
    section.main > div.block-container { position: relative; z-index: 1; }

    h1, h2, h3 {
        text-transform: uppercase;
        letter-spacing: 0.08em;
        text-shadow: 0 0 12px rgba(0,229,255,0.35);
    }

    div[data-testid="stMetric"] {
        background: rgba(0,229,255,0.04);
        border: 1px solid rgba(0,229,255,0.25);
        border-radius: 4px;
        padding: 0.75rem;
        backdrop-filter: blur(6px);
    }

    div.stButton > button, div.stFormSubmitButton > button {
        border: 1px solid rgba(0,229,255,0.6);
        box-shadow: 0 0 10px rgba(0,229,255,0.35);
        transition: box-shadow 0.15s ease;
    }
    div.stButton > button:hover, div.stFormSubmitButton > button:hover {
        box-shadow: 0 0 18px rgba(0,229,255,0.65);
        border-color: #00e5ff;
    }

    div[data-testid="stDataFrame"], div[data-testid="stTable"] {
        border: 1px solid rgba(0,229,255,0.15);
        border-radius: 4px;
        backdrop-filter: blur(6px);
    }

    /* --- 3D scene: perspective grid, holographic orb, floating shards, HUD ---
       Real DOM nodes (pseudo-elements can't host multiple 3D children), but
       negative z-index so it can never paint over Streamlit's own widget
       divs regardless of where in the DOM this markdown call lands. Reuses
       the existing --mx/--my custom properties for the orb's parallax -
       no new mousemove listener. */
    .bg-scene {
        position: fixed;
        inset: 0;
        z-index: -1;
        pointer-events: none;
        overflow: hidden;
        perspective: 1400px;
    }

    .bg-grid-plane {
        position: absolute;
        left: 50%;
        bottom: -10%;
        width: 220vw;
        height: 140vh;
        margin-left: -110vw;
        background-image:
            repeating-linear-gradient(90deg, rgba(0,229,255,0.14) 0px, rgba(0,229,255,0.14) 1px, transparent 1px, transparent 64px),
            repeating-linear-gradient(0deg, rgba(0,229,255,0.14) 0px, rgba(0,229,255,0.14) 1px, transparent 1px, transparent 64px);
        transform: rotateX(72deg);
        transform-origin: bottom center;
        -webkit-mask-image: linear-gradient(to top, rgba(0,0,0,0.9) 0%, rgba(0,0,0,0.35) 35%, transparent 70%);
        mask-image: linear-gradient(to top, rgba(0,0,0,0.9) 0%, rgba(0,0,0,0.35) 35%, transparent 70%);
        opacity: 0.55;
    }

    .bg-orb-parallax {
        position: absolute;
        top: 12%;
        right: 8%;
        width: 260px;
        height: 260px;
        transform: translate3d(calc((var(--mx, 50vw) - 50vw) * 0.015), calc((var(--my, 30vh) - 30vh) * 0.015), 0);
        transition: transform 0.5s ease-out;
        transform-style: preserve-3d;
    }
    .bg-orb {
        position: relative;
        width: 100%;
        height: 100%;
        transform-style: preserve-3d;
        animation: orb-float 10s ease-in-out infinite alternate;
    }
    @keyframes orb-float {
        from { transform: translateY(0); }
        to   { transform: translateY(-14px); }
    }
    .bg-orb-core {
        position: absolute;
        inset: 20%;
        border-radius: 50%;
        background: radial-gradient(circle at 35% 30%, rgba(120,220,255,0.55), rgba(80,120,255,0.12) 55%, transparent 75%);
        box-shadow: 0 0 60px 10px rgba(0,229,255,0.22);
    }
    .bg-orb-ring {
        position: absolute;
        inset: 0;
        border-radius: 50%;
        border: 1px solid rgba(0,229,255,0.35);
    }
    .bg-orb-ring.ring2 { border-color: rgba(139,92,246,0.3); }
    .bg-orb-ring.ring3 { inset: -12%; border-color: rgba(79,70,229,0.28); }
    .bg-orb-ring.ring1 { animation: ring-spin-1 22s linear infinite; }
    .bg-orb-ring.ring2 { animation: ring-spin-2 30s linear infinite; }
    .bg-orb-ring.ring3 { animation: ring-spin-3 38s linear infinite; }
    @keyframes ring-spin-1 { from { transform: rotateX(75deg) rotateZ(0deg); } to { transform: rotateX(75deg) rotateZ(360deg); } }
    @keyframes ring-spin-2 { from { transform: rotateX(75deg) rotateZ(60deg); } to { transform: rotateX(75deg) rotateZ(420deg); } }
    @keyframes ring-spin-3 { from { transform: rotateX(75deg) rotateZ(120deg); } to { transform: rotateX(75deg) rotateZ(480deg); } }

    .bg-shard {
        position: absolute;
        border: 1px solid rgba(0,229,255,0.25);
        border-radius: 6px;
        background: linear-gradient(135deg, rgba(0,229,255,0.06), rgba(139,92,246,0.04));
    }
    .bg-shard.shard1 {
        width: 120px; height: 80px; top: 22%; left: 8%; opacity: 0.5;
        animation: shard-float-1 12s ease-in-out infinite alternate;
    }
    .bg-shard.shard2 {
        width: 90px; height: 90px; top: 60%; left: 14%; opacity: 0.4;
        border-color: rgba(139,92,246,0.22); filter: blur(1px);
        animation: shard-float-2 15s ease-in-out infinite alternate;
    }
    .bg-shard.shard3 {
        width: 60px; height: 60px; top: 40%; right: 22%; opacity: 0.3;
        border-color: rgba(79,70,229,0.2); filter: blur(2px);
        animation: shard-float-3 18s ease-in-out infinite alternate;
    }
    @keyframes shard-float-1 { from { transform: rotate3d(1,1,0,35deg) translateY(0); } to { transform: rotate3d(1,1,0,42deg) translateY(-16px); } }
    @keyframes shard-float-2 { from { transform: rotate3d(1,-1,0.3,-25deg) translateY(0); } to { transform: rotate3d(1,-1,0.3,-18deg) translateY(14px); } }
    @keyframes shard-float-3 { from { transform: rotate3d(0.5,1,0,20deg) translateY(0); } to { transform: rotate3d(0.5,1,0,28deg) translateY(-10px); } }

    .hud-circle {
        position: absolute;
        border: 1px solid rgba(0,229,255,0.18);
        border-radius: 50%;
    }
    .hud-circle.c1 { width: 340px; height: 340px; top: 8%; right: 2%; }
    .hud-circle.c2 { width: 160px; height: 160px; top: 18%; right: 10%; border-color: rgba(168,85,247,0.15); }
    .hud-line {
        position: absolute;
        height: 1px;
        background: linear-gradient(90deg, transparent, rgba(0,229,255,0.2), transparent);
    }
    .hud-line.l1 { width: 260px; top: 30%; left: 4%; }
    .hud-line.l2 { width: 180px; top: 68%; right: 16%; }

    @media (prefers-reduced-motion: reduce) {
        div[data-testid="stAppViewContainer"]::before,
        .bg-orb, .bg-orb-ring, .bg-shard {
            animation: none;
        }
        .bg-orb-parallax { transition: none; }
    }
    </style>
    """,
    unsafe_allow_html=True,
)

st.markdown(
    """
    <div class="bg-scene" aria-hidden="true">
        <div class="bg-grid-plane"></div>
        <div class="hud-circle c1"></div>
        <div class="hud-circle c2"></div>
        <div class="hud-line l1"></div>
        <div class="hud-line l2"></div>
        <div class="bg-shard shard1"></div>
        <div class="bg-shard shard2"></div>
        <div class="bg-shard shard3"></div>
        <div class="bg-orb-parallax">
            <div class="bg-orb">
                <div class="bg-orb-core"></div>
                <div class="bg-orb-ring ring1"></div>
                <div class="bg-orb-ring ring2"></div>
                <div class="bg-orb-ring ring3"></div>
            </div>
        </div>
    </div>
    """,
    unsafe_allow_html=True,
)

# Cursor-following glow: st.markdown strips <script>, so the mousemove
# listener runs inside a components.v1 iframe and reaches into the parent
# document (same-origin under Streamlit) to set --mx/--my custom properties
# that the ::before radial-gradient above reads.
components.html(
    """
    <script>
    const doc = window.parent.document;
    if (!doc.__cursorGlowAttached) {
        doc.__cursorGlowAttached = true;
        doc.addEventListener('mousemove', (e) => {
            doc.documentElement.style.setProperty('--mx', e.clientX + 'px');
            doc.documentElement.style.setProperty('--my', e.clientY + 'px');
        });
    }
    </script>
    """,
    height=0,
)


def init_state() -> None:
    defaults = {
        "identified": False,
        "user_id": None,
        "display_name": None,
        "connected_account_id": None,
        "connection_status": "NOT_CONNECTED",
        "connected_email": None,
        "pending_redirect_url": None,
        "scan_result": None,
        "overrides": {},
        "dry_run": True,
        "cleanup_summary": None,
    }
    for key, value in defaults.items():
        st.session_state.setdefault(key, value)


def render_signin(config) -> None:
    st.header("Sign In")
    st.caption("Name/email + a passcode you choose - reusing both resumes your Gmail connection.")
    with st.form("signin_form"):
        identifier = st.text_input("Your name or email")
        passcode = st.text_input("Personal passcode", type="password")
        submitted = st.form_submit_button("Continue")

    if not submitted:
        return
    if not identifier.strip() or not passcode:
        st.error("Both fields are required.")
        return

    user_id = derive_user_id(identifier, passcode)
    st.session_state.user_id = user_id
    st.session_state.display_name = identifier.strip()
    st.session_state.identified = True

    try:
        composio = ComposioService(config, user_id=user_id)
        info = composio.get_connection_status()
        st.session_state.connection_status = info.status
        st.session_state.connected_account_id = info.connected_account_id
        st.session_state.connected_email = info.connected_email
    except ComposioServiceError as exc:
        st.warning(f"Signed in, but could not check for an existing Gmail connection: {exc}")
    st.rerun()


def render_identity_bar() -> None:
    col1, col2 = st.columns([5, 1])
    fingerprint = (st.session_state.user_id or "")[:8]
    col1.caption(f"**{st.session_state.display_name}** · `{fingerprint}`")
    if col2.button("Sign out"):
        for key in ("user_id", "display_name", "connected_account_id", "connected_email",
                    "pending_redirect_url", "scan_result", "cleanup_summary"):
            st.session_state[key] = None
        st.session_state.identified = False
        st.session_state.connection_status = "NOT_CONNECTED"
        st.session_state.overrides = {}
        st.session_state.dry_run = True
        st.rerun()


def get_composio() -> ComposioService | None:
    config = get_config()
    if not config.composio_api_key or not config.composio_gmail_auth_config_id:
        return None
    return ComposioService(config, user_id=st.session_state.user_id)


def render_connection_section(config) -> bool:
    st.header("1. Connection")
    problems = config.validate()
    if problems:
        st.error("Missing configuration:\n\n" + "\n".join(f"- {p}" for p in problems))
        st.info("Copy .env.example to .env and fill in the required values.")
        return False

    composio = get_composio()
    col1, col2 = st.columns([3, 1])

    with col1:
        if st.session_state.connection_status == "ACTIVE":
            st.success(f"Connected as **{st.session_state.connected_email or 'Gmail account'}**")
        elif st.session_state.pending_redirect_url:
            st.warning("Authorization pending. Open the link below, complete Google's OAuth "
                       "consent, then click 'I've authorized - check status'.")
            st.markdown(f"[Authorize Gmail access]({st.session_state.pending_redirect_url})")
        else:
            st.info("Not connected. Each user connects their own Gmail account - "
                    "no credentials are shared.")

    with col2:
        if st.session_state.connection_status != "ACTIVE":
            if st.session_state.pending_redirect_url:
                if st.button("I've authorized - check status"):
                    try:
                        info = composio.wait_for_connection(
                            st.session_state.connected_account_id, timeout=15.0
                        )
                        st.session_state.connection_status = info.status
                        st.session_state.connected_email = info.connected_email
                        if info.status != "ACTIVE":
                            st.warning(f"Status: {info.status}. Try again after authorizing.")
                    except Exception as exc:  # noqa: BLE001
                        st.error(f"Could not confirm connection: {exc}")
                    st.rerun()
            else:
                if st.button("Connect Gmail"):
                    try:
                        info = composio.start_connection()
                        st.session_state.connected_account_id = info.connected_account_id
                        st.session_state.pending_redirect_url = info.redirect_url
                    except ComposioServiceError as exc:
                        st.error(f"Could not start connection: {exc}")
                    st.rerun()
        else:
            if st.button("Disconnect"):
                try:
                    composio.disconnect(st.session_state.connected_account_id)
                except Exception as exc:  # noqa: BLE001
                    st.error(f"Disconnect failed: {exc}")
                for key in ("connected_account_id", "connection_status", "connected_email",
                            "pending_redirect_url", "scan_result", "cleanup_summary"):
                    st.session_state[key] = None
                st.session_state.connection_status = "NOT_CONNECTED"
                st.rerun()

    return st.session_state.connection_status == "ACTIVE"


_DATE_RANGE_PRESETS = {
    "All time": None,
    "Last 7 days": 7,
    "Last 30 days": 30,
    "Last 90 days": 90,
    "Custom range": "custom",
}


def _build_date_query(preset: str, custom_start: date | None, custom_end: date | None) -> str | None:
    """Gmail search query for the chosen range. `before:` is exclusive, so
    the end date is bumped by a day to make the selected end date inclusive."""
    days = _DATE_RANGE_PRESETS[preset]
    if days is None:
        return None
    if days == "custom":
        if not custom_start or not custom_end:
            return None
        start, end = custom_start, custom_end
    else:
        end = date.today()
        start = end - timedelta(days=days)
    end_exclusive = end + timedelta(days=1)
    return f"after:{start.strftime('%Y/%m/%d')} before:{end_exclusive.strftime('%Y/%m/%d')}"


def render_scan_controls(config) -> None:
    st.header("2. Scan Inbox")
    col1, col2, col3 = st.columns([2, 2, 2])
    with col1:
        max_messages = st.number_input(
            "Max messages to scan", min_value=10, max_value=5000,
            value=config.max_messages_per_scan, step=50,
        )
    with col2:
        st.session_state.dry_run = st.checkbox(
            "Dry Run (no Gmail changes)", value=st.session_state.dry_run,
            help="While enabled, cleanup only simulates actions and logs results.",
        )
    with col3:
        scan_clicked = st.button("Scan Inbox", type="primary")

    date_col1, date_col2, date_col3 = st.columns([2, 2, 2])
    with date_col1:
        date_preset = st.selectbox("Date range", list(_DATE_RANGE_PRESETS.keys()))
    custom_start = custom_end = None
    if date_preset == "Custom range":
        with date_col2:
            custom_start = st.date_input("From", value=date.today() - timedelta(days=30))
        with date_col3:
            custom_end = st.date_input("To", value=date.today())

    if scan_clicked:
        config.max_messages_per_scan = int(max_messages)
        date_query = _build_date_query(date_preset, custom_start, custom_end)
        if date_preset == "Custom range" and not date_query:
            st.error("Pick both a From and To date for a custom range.")
            return
        composio = get_composio()
        gmail = GmailService(composio, st.session_state.connected_account_id)
        classifier = EmailClassifier(config)
        with st.spinner("Fetching metadata and classifying..."):
            try:
                result = scan_and_classify(gmail, classifier, config, date_query=date_query)
                st.session_state.scan_result = result
                st.session_state.overrides = {}
                st.session_state.cleanup_summary = None
                st.session_state.usage = classifier.usage.as_dict()
            except Exception as exc:  # noqa: BLE001
                st.error(f"Scan failed: {exc}")
                logger.exception("scan failed")


def render_inbox_overview(scan_result) -> None:
    st.header("Inbox Overview")
    cols = st.columns(4)
    cols[0].metric("Total inbox messages", scan_result.total_inbox_count)
    cols[1].metric("Unread", scan_result.unread_count)
    cols[2].metric("Emails scanned", scan_result.scanned_count)
    date_range = "-"
    if scan_result.date_range_start and scan_result.date_range_end:
        date_range = (f"{scan_result.date_range_start.date()} to "
                      f"{scan_result.date_range_end.date()}")
    cols[3].metric("Date range scanned", date_range)

    if "usage" in st.session_state:
        usage = st.session_state.usage
        st.caption(
            f"Gemini API usage this scan: {usage['calls']} calls, "
            f"{usage['input_tokens']} input tokens, {usage['output_tokens']} output tokens."
        )


# Banking mail gets the full Purpose/Context hierarchy (that's the whole
# point - one bank spans Transactions/Security/Promotions/etc). Everything
# else collapses into a handful of coarse buckets for the UI so the
# breakdown/recommendations views don't explode into one row per sender.
# This is a display-only grouping - decide_action/safety_rules still runs
# per-message against the full fine-grained category, unaffected by this.
_COARSE_UI_GROUPS = {
    Category.SECURITY_ALERTS: "Security & Verification",
    Category.OTP_VERIFICATION: "Security & Verification",
    Category.ORDERS_PURCHASES: "Orders & Shopping",
    Category.SHIPPING_DELIVERY: "Orders & Shopping",
    Category.PROMOTIONS_MARKETING: "Promotions",
    Category.NEWSLETTERS: "Promotions",
    Category.APP_SYSTEM_NOTIFICATIONS: "Notifications",
    Category.ACCOUNT_SERVICE_UPDATES: "Notifications",
    Category.SUBSCRIPTIONS: "Notifications",
    Category.SOCIAL: "Notifications",
    Category.PERSONAL: "Personal & Work",
    Category.WORK: "Personal & Work",
    Category.TRAVEL: "Personal & Work",
    Category.TRANSACTIONS: "Finance",
    Category.STATEMENTS_DOCUMENTS: "Finance",
    Category.BILLS_PAYMENTS: "Finance",
    Category.FINANCE_INVESTMENT: "Finance",
    Category.UNCERTAIN: "Other",
    Category.OTHER: "Other",
    Category.SPAM_SUSPICIOUS: "Other",
}


def ui_group(classification) -> str:
    if classification.context == "Banking":
        return classification.display_category
    return _COARSE_UI_GROUPS.get(classification.category, "Other")


def render_classification_breakdown(recommendations) -> None:
    st.header("3. AI Classification")
    st.caption("Banking: full Purpose/Context breakdown. Everything else: coarse buckets.")
    counts = Counter(ui_group(r.classification) for r in recommendations)
    df = pd.DataFrame(
        [{"Category": cat, "Count": count} for cat, count in counts.most_common()]
    )
    st.bar_chart(df.set_index("Category"))
    st.dataframe(df, width="stretch", hide_index=True)


def render_recommendations(recommendations, config) -> None:
    st.header("4. Cleanup Recommendations")
    counts_by_cat = Counter(ui_group(r.classification) for r in recommendations)
    default_action_by_cat = {}
    for cat in counts_by_cat:
        sample = next(r for r in recommendations if ui_group(r.classification) == cat)
        default_action_by_cat[cat] = sample.recommended_action

    for cat, count in counts_by_cat.most_common():
        col1, col2, col3 = st.columns([3, 1, 2])
        col1.write(f"**{cat}** ({count} emails)")
        col2.write(default_action_by_cat[cat])
        options = [Action.KEEP, Action.ARCHIVE, Action.TRASH, Action.REVIEW]
        override = col3.selectbox(
            "Override", options,
            index=options.index(default_action_by_cat[cat]) if default_action_by_cat[cat] in options else 0,
            key=f"override_{cat}", label_visibility="collapsed",
        )
        if override != default_action_by_cat[cat]:
            st.session_state.overrides[cat] = override
        elif cat in st.session_state.overrides:
            del st.session_state.overrides[cat]


def apply_overrides(recommendations) -> None:
    for rec in recommendations:
        override = st.session_state.overrides.get(ui_group(rec.classification))
        rec.user_override = override if override else None


def render_preview(recommendations) -> None:
    st.header("5. Preview")
    apply_overrides(recommendations)

    categories = sorted({ui_group(r.classification) for r in recommendations})
    actions = sorted({r.user_override or r.recommended_action for r in recommendations})
    col1, col2 = st.columns(2)
    cat_filter = col1.multiselect("Filter by category", categories, default=[])
    action_filter = col2.multiselect("Filter by action", actions, default=[])

    filtered = recommendations
    if cat_filter:
        filtered = [r for r in filtered if ui_group(r.classification) in cat_filter]
    if action_filter:
        filtered = [r for r in filtered if (r.user_override or r.recommended_action) in action_filter]

    rows = [{
        "Sender": r.email.sender,
        "Subject": r.email.subject,
        "Received": r.email.received_at.date(),
        "Purpose": r.classification.category,
        "Context": r.classification.context,
        "Detailed Type": r.classification.detailed_type,
        "Action": r.user_override or r.recommended_action,
        "Confidence": round(r.classification.confidence, 2),
        "Reason": r.protection_reason or r.classification.reason,
    } for r in filtered[:500]]
    st.caption(f"Showing {len(rows)} of {len(filtered)} filtered messages (500 max).")
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)


def render_confirmation(recommendations, config, gmail) -> None:
    st.header("6. Confirmation")
    apply_overrides(recommendations)

    final_actions = [r.user_override or r.recommended_action for r in recommendations]
    n_archive = final_actions.count(Action.ARCHIVE)
    n_trash = final_actions.count(Action.TRASH)
    n_keep = final_actions.count(Action.KEEP)
    n_review = final_actions.count(Action.REVIEW)

    cols = st.columns(4)
    cols[0].metric("Will be archived", n_archive)
    cols[1].metric("Will be moved to Trash", n_trash)
    cols[2].metric("Will be kept", n_keep)
    cols[3].metric("Manual review / skipped", n_review)

    if st.session_state.dry_run:
        st.info("Dry Run is ON. No Gmail changes will be made regardless of confirmation.")

    trash_confirmed = False
    if n_trash > 0:
        trash_confirmed = st.checkbox(
            f"I confirm moving {n_trash} message(s) to Trash (recoverable in Gmail Trash for 30 days)"
        )

    if st.button("Execute Cleanup", type="primary"):
        audit = AuditLogger(
            st.session_state.connected_email or st.session_state.display_name or st.session_state.user_id
        )
        with st.spinner("Applying actions..."):
            try:
                summary = execute_cleanup(
                    gmail, recommendations, config, audit,
                    trash_confirmed=trash_confirmed,
                )
                st.session_state.cleanup_summary = summary
            except Exception as exc:  # noqa: BLE001
                st.error(f"Cleanup failed: {exc}")
                logger.exception("cleanup failed")


def render_results(summary) -> None:
    st.header("7. Results")
    cols = st.columns(4)
    cols[0].metric("Emails scanned", summary.scanned)
    cols[1].metric("Emails classified", summary.classified)
    cols[2].metric("Archived", summary.archived)
    cols[3].metric("Trashed", summary.trashed)
    cols2 = st.columns(4)
    cols2[0].metric("Protected", summary.protected)
    cols2[1].metric("Skipped", summary.skipped)
    cols2[2].metric("Manual review", summary.manual_review)
    cols2[3].metric("Errors", summary.errors)


def main() -> None:
    init_state()
    config = get_config()
    config.dry_run = st.session_state.dry_run

    st.title("Gmail Cleanup Agent")
    st.caption("Nothing is deleted without your explicit confirmation.")

    problems = config.validate()
    if problems:
        st.error("Missing configuration:\n\n" + "\n".join(f"- {p}" for p in problems))
        st.info(
            "Local dev: copy .env.example to .env and fill in the values.\n\n"
            "Streamlit Community Cloud: set them under Manage app -> Settings -> "
            "Secrets. If you already did and this still shows, your secrets.toml "
            "likely has a syntax error - check the app's deploy logs for a "
            "'st.secrets failed to load' warning with the parse error."
        )
        return

    if not st.session_state.identified:
        render_signin(config)
        return

    render_identity_bar()
    connected = render_connection_section(config)
    if not connected:
        return

    render_scan_controls(config)

    scan_result = st.session_state.scan_result
    if not scan_result:
        return

    render_inbox_overview(scan_result)
    render_classification_breakdown(scan_result.recommendations)
    render_recommendations(scan_result.recommendations, config)
    render_preview(scan_result.recommendations)

    composio = get_composio()
    gmail = GmailService(composio, st.session_state.connected_account_id)
    render_confirmation(scan_result.recommendations, config, gmail)

    if st.session_state.cleanup_summary:
        render_results(st.session_state.cleanup_summary)


if __name__ == "__main__":
    main()
