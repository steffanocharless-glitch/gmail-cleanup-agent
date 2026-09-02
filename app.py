"""AI Gmail Cleanup Agent - Streamlit entry point.

Each user authenticates their own Gmail account via Composio OAuth. Nothing
here uses a shared/pre-existing connection - every session starts unconnected.
"""
from __future__ import annotations

import subprocess
from collections import Counter
from datetime import date, timedelta

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

from src.actionable_dates import detect_actionable_date
from src.alert_messages import build_actionable_alert_message, build_daily_summary_message
from src.calendar_service import CalendarService
from src.cleanup_engine import execute_cleanup, scan_and_classify
from src.classifier import EmailClassifier
from src.composio_service import CALENDAR_TOOLKIT, ComposioService, ComposioServiceError
from src.config import PROJECT_ROOT, Action, Category, get_config
from src.gmail_service import GmailService
from src.identity import derive_user_id
from src.logger import AuditLogger, get_logger
from src.telegram_service import TelegramAlertError, get_recent_chats, send_owner_alert
from src.user_settings import ALERT_CATEGORIES, TelegramSettings, load_telegram_settings, save_telegram_settings

logger = get_logger(__name__)


def _deployed_commit() -> str:
    """Short git SHA of whatever's actually running - lets us confirm a
    Streamlit Cloud redeploy picked up the latest push just by looking at
    the page, without hunting for a build-log panel."""
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=PROJECT_ROOT, stderr=subprocess.DEVNULL, timeout=3,
        ).decode().strip()
    except Exception:  # noqa: BLE001
        return "unknown"

st.set_page_config(page_title="AI Gmail Cleanup Agent", page_icon="📬", layout="wide")

st.markdown(
    """
    <style>
    footer {visibility: hidden;}
    .block-container {padding-top: 2rem; padding-bottom: 2rem; max-width: 1100px;}
    div[data-testid="stCaptionContainer"] {opacity: 0.7;}
    hr {margin: 0.5rem 0; border-color: #cbd5e1;}

    /* Subtle dot-grid, common in enterprise dashboards (Stripe/Linear-style) -
       fixed so it doesn't scroll with content, low-contrast slate on the
       light background rather than anything saturated. */
    div[data-testid="stAppViewContainer"] {
        background-color: #f8fafc;
        background-image: radial-gradient(circle, rgba(71,85,105,0.16) 1px, transparent 1px);
        background-size: 22px 22px;
        background-attachment: fixed;
        position: relative;
    }
    /* Cursor spotlight: a second, larger/brighter dot-grid on the exact
       same 22px lattice, revealed only in a circle around the cursor via
       mask - the dots there visibly grow, everywhere else stays the plain
       fine grid above. --mx/--my set by the script below; default parks
       the spotlight off-screen so nothing shows before the first move. */
    div[data-testid="stAppViewContainer"]::before {
        content: "";
        position: fixed;
        inset: 0;
        z-index: 0;
        pointer-events: none;
        background-image: radial-gradient(circle, rgba(71,85,105,0.6) 2.5px, transparent 2.5px);
        background-size: 22px 22px;
        -webkit-mask-image: radial-gradient(circle 150px at var(--mx, -9999px) var(--my, -9999px), black 0%, transparent 100%);
        mask-image: radial-gradient(circle 150px at var(--mx, -9999px) var(--my, -9999px), black 0%, transparent 100%);
    }
    /* Soft glow halo behind the enlarged dots. */
    div[data-testid="stAppViewContainer"]::after {
        content: "";
        position: fixed;
        inset: 0;
        z-index: 0;
        pointer-events: none;
        background: radial-gradient(circle 150px at var(--mx, -9999px) var(--my, -9999px), rgba(71,85,105,0.10), transparent 75%);
    }
    section.main > div.block-container { position: relative; z-index: 1; }

    div[data-testid="stMetric"] {
        background: #f1f5f9;
        border: 1px solid #cbd5e1;
        border-radius: 4px;
        padding: 0.75rem;
    }

    div[data-testid="stDataFrame"], div[data-testid="stTable"] {
        border: 1px solid #cbd5e1;
        border-radius: 4px;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

# st.markdown strips <script>, so the mousemove listener runs inside a
# components.v1 iframe and reaches into the parent document (same-origin
# under Streamlit) to set --mx/--my, which the dot-grid spotlight above reads.
components.html(
    """
    <script>
    const doc = window.parent.document;
    if (!doc.__cursorSpotlightAttached) {
        doc.__cursorSpotlightAttached = true;
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
        # Google Calendar (separate Composio connection from Gmail)
        "calendar_connected_account_id": None,
        "calendar_connection_status": "NOT_CONNECTED",
        "calendar_pending_redirect_url": None,
        "calendar_created_message_ids": set(),
        "calendar_status_checked": False,
        "calendar_connected_email": None,
        "calendar_all_active": [],  # full ACTIVE account list, only populated when there are 2+
        # Telegram (app-level bot connection, no per-user OAuth - see
        # telegram_service.py. Only the "detect my chat" candidate list
        # needs session state; the bot's own connection status is checked
        # live, not tracked here.)
        "telegram_chat_candidates": None,
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
                    "pending_redirect_url", "scan_result", "cleanup_summary",
                    "calendar_connected_account_id", "calendar_pending_redirect_url"):
            st.session_state[key] = None
        st.session_state.identified = False
        st.session_state.connection_status = "NOT_CONNECTED"
        st.session_state.calendar_connection_status = "NOT_CONNECTED"
        st.session_state.calendar_created_message_ids = set()
        st.session_state.calendar_status_checked = False
        st.session_state.calendar_connected_email = None
        st.session_state.calendar_all_active = []
        st.session_state.telegram_chat_candidates = None
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


def get_calendar_composio(config) -> ComposioService | None:
    if not config.composio_api_key or not config.composio_calendar_auth_config_id:
        return None
    return ComposioService(
        config, user_id=st.session_state.user_id,
        toolkit=CALENDAR_TOOLKIT, auth_config_id=config.composio_calendar_auth_config_id,
    )


def _apply_calendar_connection(info) -> None:
    """Single place that writes a resolved Calendar ConnectionInfo into
    session_state, so every call site (auto-check, connect-click reuse,
    picker switch) stays consistent."""
    st.session_state.calendar_connection_status = "ACTIVE"
    st.session_state.calendar_connected_account_id = info.connected_account_id
    st.session_state.calendar_connected_email = info.connected_email
    st.session_state.calendar_pending_redirect_url = None
    st.session_state.calendar_all_active = (
        [info] + info.other_active_candidates if info.other_active_candidates else []
    )


def render_calendar_connection_section(config) -> bool:
    """Separate Composio connection from Gmail - same Google account, but
    Composio scopes OAuth per toolkit, so this needs its own connect step.
    Hidden entirely if COMPOSIO_CALENDAR_AUTH_CONFIG_ID isn't configured."""
    if not config.composio_calendar_auth_config_id:
        return False

    calendar_composio = get_calendar_composio(config)

    # One-time check per session (mirrors what Gmail already does at sign-in):
    # if an ACTIVE Calendar connection already exists, show it as connected
    # immediately instead of offering a "Connect" button that would just
    # rediscover the same thing (or, before the fix below, crash).
    if not st.session_state.calendar_status_checked:
        try:
            info = calendar_composio.get_connection_status(prefer_email=st.session_state.connected_email)
            if info.status == "ACTIVE":
                _apply_calendar_connection(info)
        except ComposioServiceError as exc:
            st.warning(f"Could not check for an existing Calendar connection: {exc}")
        st.session_state.calendar_status_checked = True

    st.subheader("Google Calendar")
    col1, col2 = st.columns([3, 1])

    with col1:
        if st.session_state.calendar_connection_status == "ACTIVE":
            st.success(f"Calendar connected{' as ' + st.session_state.calendar_connected_email if st.session_state.calendar_connected_email else ''}")
            all_active = st.session_state.calendar_all_active
            others = [a for a in all_active if a.connected_account_id != st.session_state.calendar_connected_account_id]
            if others:
                st.caption(f"{len(others)} other active Calendar connection(s) also exist for this account.")
                labels = {(a.connected_email or a.connected_account_id): a for a in all_active}
                current_label = next(
                    (k for k, a in labels.items() if a.connected_account_id == st.session_state.calendar_connected_account_id),
                    None,
                )
                choice = st.selectbox(
                    "Which connection to use?", list(labels.keys()),
                    index=list(labels.keys()).index(current_label) if current_label in labels else 0,
                    key="cal_candidate_pick",
                )
                if labels[choice].connected_account_id != st.session_state.calendar_connected_account_id:
                    if st.button("Switch to this connection"):
                        st.session_state.calendar_connected_account_id = labels[choice].connected_account_id
                        st.session_state.calendar_connected_email = labels[choice].connected_email
                        st.rerun()
        elif st.session_state.calendar_pending_redirect_url:
            st.warning("Authorization pending. Open the link below, complete Google's OAuth "
                       "consent, then click 'I've authorized - check status'.")
            st.markdown(f"[Authorize Calendar access]({st.session_state.calendar_pending_redirect_url})")
        else:
            st.info("Not connected. Needed to turn actionable-date suggestions into real events.")

    with col2:
        if st.session_state.calendar_connection_status != "ACTIVE":
            if st.session_state.calendar_pending_redirect_url:
                if st.button("I've authorized - check status", key="cal_check_status"):
                    try:
                        info = calendar_composio.wait_for_connection(
                            st.session_state.calendar_connected_account_id, timeout=15.0
                        )
                        if info.status == "ACTIVE":
                            _apply_calendar_connection(info)
                        else:
                            st.session_state.calendar_connection_status = info.status
                            st.warning(f"Status: {info.status}. Try again after authorizing.")
                    except Exception as exc:  # noqa: BLE001
                        st.error(f"Could not confirm connection: {exc}")
                    st.rerun()
            else:
                if st.button("Connect Google Calendar"):
                    try:
                        info = calendar_composio.start_connection()
                        if info.status == "ACTIVE":
                            # Reused an existing connection - no OAuth needed.
                            _apply_calendar_connection(info)
                        else:
                            st.session_state.calendar_connected_account_id = info.connected_account_id
                            st.session_state.calendar_pending_redirect_url = info.redirect_url
                    except ComposioServiceError as exc:
                        st.error(f"Could not start connection: {exc}")
                    st.rerun()
        else:
            if st.button("Disconnect Calendar"):
                try:
                    calendar_composio.disconnect(st.session_state.calendar_connected_account_id)
                except Exception as exc:  # noqa: BLE001
                    st.error(f"Disconnect failed: {exc}")
                st.session_state.calendar_connected_account_id = None
                st.session_state.calendar_connected_email = None
                st.session_state.calendar_pending_redirect_url = None
                st.session_state.calendar_connection_status = "NOT_CONNECTED"
                st.session_state.calendar_all_active = []
                st.session_state.calendar_status_checked = True  # we know it's gone, no need to re-check
                st.rerun()

    return st.session_state.calendar_connection_status == "ACTIVE"


def render_telegram_settings(config) -> None:
    """No per-user OAuth here (Telegram bots use a token, not OAuth - see
    telegram_service.py) - the bot connects itself lazily on first send.
    What this section actually does: let the user pick which Telegram
    chat is theirs, and which alert categories they want."""
    if not (config.composio_telegram_auth_config_id and config.telegram_bot_token):
        return

    st.subheader("Telegram Alerts")
    settings = load_telegram_settings(st.session_state.user_id)

    if settings.chat_id:
        st.success(f"Alerts go to: {settings.display_label or settings.chat_id}")
    else:
        st.info(
            "Not set up yet. In Telegram: open the Cleanup Agent bot, press Start, "
            "then come back and click 'Detect My Telegram Chat' below."
        )

    if st.button("Detect My Telegram Chat"):
        try:
            st.session_state.telegram_chat_candidates = get_recent_chats(config)
        except TelegramAlertError as exc:
            st.error(str(exc))
        except ComposioServiceError as exc:
            st.error(f"Could not reach the Telegram bot: {exc}")

    candidates = st.session_state.telegram_chat_candidates
    if candidates is not None:
        if not candidates:
            st.warning("No recent chats found. Message the bot (press Start) first, then try again.")
        else:
            options = {f"{c['label']} ({c['chat_id']})": c for c in candidates}
            choice = st.selectbox("Which chat is yours?", list(options.keys()), key="tg_chat_choice")
            if st.button("Confirm This Chat"):
                picked = options[choice]
                save_telegram_settings(st.session_state.user_id, TelegramSettings(
                    enabled=True, chat_id=picked["chat_id"], display_label=picked["label"],
                    categories=settings.categories,
                ))
                st.session_state.telegram_chat_candidates = None
                st.success("Saved.")
                st.rerun()

    if not settings.chat_id:
        return

    with st.form("telegram_settings_form"):
        enabled = st.checkbox("Enable Telegram alerts", value=settings.enabled)
        st.caption("Preferences")
        new_categories = {}
        for key, label in ALERT_CATEGORIES.items():
            new_categories[key] = st.checkbox(label, value=settings.categories.get(key, False), key=f"tg_pref_{key}")
        saved = st.form_submit_button("Save Telegram Settings")

    if saved:
        save_telegram_settings(st.session_state.user_id, TelegramSettings(
            enabled=enabled, chat_id=settings.chat_id, display_label=settings.display_label,
            categories=new_categories,
        ))
        st.success("Saved.")
        st.rerun()

    col1, col2 = st.columns(2)
    with col1:
        if settings.enabled and st.button("Send Test Alert"):
            try:
                send_owner_alert(config, st.session_state.user_id, "test", "Cleanup Agent Alert\n\nThis is a test alert.")
                st.success("Test alert sent.")
            except TelegramAlertError as exc:
                st.error(str(exc))
    with col2:
        if st.button("Disconnect Telegram (clear my settings)"):
            save_telegram_settings(st.session_state.user_id, TelegramSettings())
            st.rerun()


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


def _telegram_category_for(mail_category: str) -> str:
    if mail_category == Category.BILLS_PAYMENTS:
        return "bills_payments"
    if mail_category == Category.SUBSCRIPTIONS:
        return "subscription_renewals"
    return "deadlines"


def render_action_center(recommendations, config) -> None:
    """Actionable-date suggestions (bills due, renewals, interviews...) from
    this scan. Never auto-creates events, never auto-sends alerts - every
    suggestion needs an explicit opt-in/click. Independent of the
    Purpose/Context classification; runs its own content detector."""
    st.header("6. Action Center")

    calendar_connected = st.session_state.calendar_connection_status == "ACTIVE"
    telegram_ready = False
    tg_settings = None
    if config.composio_telegram_auth_config_id and config.telegram_bot_token:
        tg_settings = load_telegram_settings(st.session_state.user_id)
        telegram_ready = tg_settings.enabled and bool(tg_settings.chat_id)

    already_created = st.session_state.calendar_created_message_ids
    suggestions = [
        (s, r.classification.category) for r in recommendations
        if (s := detect_actionable_date(r.email)) and s.message_id not in already_created
    ]

    if not suggestions:
        st.caption("No actionable dates (bills due, renewals, interviews...) found in this scan.")
        return

    st.caption(f"{len(suggestions)} Calendar Suggestion(s) found. Nothing is created or sent until you act on each one.")

    selected: list[tuple] = []
    for s, mail_category in suggestions:
        with st.container(border=True):
            col1, col2, col3, col4 = st.columns([3, 2, 1, 2])
            with col1:
                st.write(f"**{s.summary}**")
                if not s.date_is_certain:
                    st.caption("Date not confidently detected - please review before including.")
            with col2:
                chosen_date = st.date_input(
                    "Event date", value=s.suggested_date,
                    key=f"cal_date_{s.message_id}", label_visibility="collapsed",
                )
            with col3:
                include = st.checkbox(
                    "Include", value=s.date_is_certain, key=f"cal_sel_{s.message_id}",
                )
            with col4:
                alert_category = _telegram_category_for(mail_category)
                can_alert = telegram_ready and tg_settings.category_enabled(alert_category)
                if can_alert and st.button("Send Telegram Alert", key=f"tg_alert_{s.message_id}"):
                    try:
                        send_owner_alert(config, st.session_state.user_id, alert_category,
                                          build_actionable_alert_message(s))
                        st.success("Sent.")
                    except TelegramAlertError as exc:
                        st.error(str(exc))
            if include:
                selected.append((s, chosen_date))

    if not selected:
        return

    if not calendar_connected:
        st.info("Connect Google Calendar above to create these events.")
        return

    if st.button(f"Add {len(selected)} to Calendar", type="primary"):
        calendar_composio = get_calendar_composio(config)
        calendar = CalendarService(calendar_composio, st.session_state.calendar_connected_account_id)
        events = [
            {
                "op_id": f"op_{i}",
                "summary": s.summary[:200],
                "description": f"Created by Gmail Cleanup Agent from message {s.message_id}.",
                "start_date": chosen_date,
                "end_date": chosen_date + timedelta(days=1),
            }
            for i, (s, chosen_date) in enumerate(selected)
        ]
        try:
            calendar.create_all_day_events(events)
            st.session_state.calendar_created_message_ids |= {s.message_id for s, _ in selected}
            st.success(f"Created {len(selected)} calendar event(s).")
            st.rerun()
        except ComposioServiceError as exc:
            st.error(f"Could not create events: {exc}")


def render_daily_summary_button(scan_result, config) -> None:
    """Manual trigger only - this app has no background scheduler (a
    Streamlit app only runs while a page is open), so a true recurring
    daily summary needs an external cron hitting some endpoint. Out of
    scope here; this sends one summary, on demand, for the current scan."""
    if not (config.composio_telegram_auth_config_id and config.telegram_bot_token):
        return
    settings = load_telegram_settings(st.session_state.user_id)
    if not (settings.enabled and settings.chat_id and settings.category_enabled("daily_summary")):
        return

    counts = Counter(ui_group(r.classification) for r in scan_result.recommendations)
    n_suggestions = sum(1 for r in scan_result.recommendations if detect_actionable_date(r.email))

    if st.button("Send Telegram Daily Summary"):
        message = build_daily_summary_message(
            scan_result.scanned_count,
            {**dict(counts.most_common(5)), "Calendar suggestions": n_suggestions},
        )
        try:
            send_owner_alert(config, st.session_state.user_id, "daily_summary", message)
            st.success("Daily summary sent.")
        except TelegramAlertError as exc:
            st.error(str(exc))


def render_confirmation(recommendations, config, gmail) -> None:
    st.header("7. Confirmation")
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
    st.header("8. Results")
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
    st.caption(f"Nothing is deleted without your explicit confirmation. · build `{_deployed_commit()}`")

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
    if config.composio_calendar_auth_config_id:
        render_calendar_connection_section(config)
    if config.composio_telegram_auth_config_id and config.telegram_bot_token:
        render_telegram_settings(config)
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
    telegram_configured = config.composio_telegram_auth_config_id and config.telegram_bot_token
    if config.composio_calendar_auth_config_id or telegram_configured:
        render_action_center(scan_result.recommendations, config)
    if telegram_configured:
        render_daily_summary_button(scan_result, config)

    composio = get_composio()
    gmail = GmailService(composio, st.session_state.connected_account_id)
    render_confirmation(scan_result.recommendations, config, gmail)

    if st.session_state.cleanup_summary:
        render_results(st.session_state.cleanup_summary)


if __name__ == "__main__":
    main()
