"""AI Gmail Cleanup Agent - Streamlit entry point.

Each user authenticates their own Gmail account via Composio OAuth. Nothing
here uses a shared/pre-existing connection - every session starts unconnected.
"""
from __future__ import annotations

import uuid
from collections import Counter

import pandas as pd
import streamlit as st

from src.cleanup_engine import execute_cleanup, scan_and_classify
from src.classifier import EmailClassifier
from src.composio_service import ComposioService, ComposioServiceError
from src.config import Action, Category, get_config
from src.gmail_service import GmailService
from src.logger import AuditLogger, get_logger

logger = get_logger(__name__)

st.set_page_config(page_title="AI Gmail Cleanup Agent", page_icon="📬", layout="wide")


def init_state() -> None:
    defaults = {
        "user_id": str(uuid.uuid4()),
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

    if scan_clicked:
        config.max_messages_per_scan = int(max_messages)
        composio = get_composio()
        gmail = GmailService(composio, st.session_state.connected_account_id)
        classifier = EmailClassifier(config)
        with st.spinner("Fetching metadata and classifying..."):
            try:
                result = scan_and_classify(gmail, classifier, config)
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


def render_classification_breakdown(recommendations) -> None:
    st.header("3. AI Classification")
    counts = Counter(r.classification.category for r in recommendations)
    df = pd.DataFrame(
        [{"Category": cat, "Count": counts.get(cat, 0)} for cat in Category.ALL]
    ).sort_values("Count", ascending=False)
    st.bar_chart(df.set_index("Category"))
    st.dataframe(df, width="stretch", hide_index=True)


def render_recommendations(recommendations, config) -> None:
    st.header("4. Cleanup Recommendations")
    counts_by_cat = Counter(r.classification.category for r in recommendations)
    default_action_by_cat = {}
    for cat in Category.ALL:
        sample = next((r for r in recommendations if r.classification.category == cat), None)
        default_action_by_cat[cat] = sample.recommended_action if sample else "-"

    for cat in Category.ALL:
        if counts_by_cat.get(cat, 0) == 0:
            continue
        col1, col2, col3 = st.columns([3, 1, 2])
        col1.write(f"**{cat}** ({counts_by_cat[cat]} emails)")
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
        override = st.session_state.overrides.get(rec.classification.category)
        rec.user_override = override if override else None


def render_preview(recommendations) -> None:
    st.header("5. Preview")
    apply_overrides(recommendations)

    categories = sorted({r.classification.category for r in recommendations})
    actions = sorted({r.user_override or r.recommended_action for r in recommendations})
    col1, col2 = st.columns(2)
    cat_filter = col1.multiselect("Filter by category", categories, default=[])
    action_filter = col2.multiselect("Filter by action", actions, default=[])

    filtered = recommendations
    if cat_filter:
        filtered = [r for r in filtered if r.classification.category in cat_filter]
    if action_filter:
        filtered = [r for r in filtered if (r.user_override or r.recommended_action) in action_filter]

    rows = [{
        "Sender": r.email.sender,
        "Subject": r.email.subject,
        "Received": r.email.received_at.date(),
        "Category": r.classification.category,
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
        audit = AuditLogger(st.session_state.connected_email or st.session_state.user_id)
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

    st.title("📬 AI Gmail Cleanup Agent")
    st.caption("Connect your own Gmail account. Nothing is deleted without your explicit confirmation.")

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
