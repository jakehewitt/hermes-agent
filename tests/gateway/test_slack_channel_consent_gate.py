"""Tests for the Slack channel consent gate (channel_consent_gate config).

Contract:
  - Gate disabled (default) → join posts no prompt, messages flow (stock).
  - Gate enabled → bot join marks the channel pending + posts the
    Activate/Decline Block Kit prompt; messages in pending/declined
    channels are dropped before any processing.
  - Activate click → approved, messages flow; Decline → stays dormant.
  - Re-invite to an approved channel does NOT reset consent.
  - DMs are never gated.
  - Untracked channels (joined before the gate existed) are never gated.
"""
import json

from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig
from gateway.platforms.slack import SlackAdapter
from gateway.platforms.slack_consent import ChannelConsentStore


# ---------------------------------------------------------------------------
# ChannelConsentStore
# ---------------------------------------------------------------------------


class TestChannelConsentStore:
    def test_untracked_channel_is_not_dormant(self, tmp_path):
        store = ChannelConsentStore(tmp_path / "consent.json")
        assert store.status("C1") is None
        assert store.is_dormant("C1") is False

    def test_pending_and_declined_are_dormant_approved_is_not(self, tmp_path):
        store = ChannelConsentStore(tmp_path / "consent.json")
        store.set("C1", "pending")
        assert store.is_dormant("C1") is True
        store.set("C1", "approved", by_user_id="U1", by_user_name="jake")
        assert store.is_dormant("C1") is False
        store.set("C1", "declined")
        assert store.is_dormant("C1") is True

    def test_persists_across_instances(self, tmp_path):
        path = tmp_path / "consent.json"
        ChannelConsentStore(path).set("C1", "approved", by_user_id="U1")
        store2 = ChannelConsentStore(path)
        assert store2.status("C1") == "approved"

    def test_corrupt_file_degrades_to_empty(self, tmp_path):
        path = tmp_path / "consent.json"
        path.write_text("{not json", encoding="utf-8")
        store = ChannelConsentStore(path)
        assert store.status("C1") is None
        # And writes still work afterwards
        store.set("C1", "pending")
        assert ChannelConsentStore(path).status("C1") == "pending"

    def test_invalid_states_in_file_are_dropped(self, tmp_path):
        path = tmp_path / "consent.json"
        path.write_text(
            json.dumps({"C1": {"status": "bogus"}, "C2": {"status": "pending"}}),
            encoding="utf-8",
        )
        store = ChannelConsentStore(path)
        assert store.status("C1") is None
        assert store.status("C2") == "pending"

    def test_set_rejects_invalid_status(self, tmp_path):
        store = ChannelConsentStore(tmp_path / "consent.json")
        with pytest.raises(ValueError):
            store.set("C1", "maybe")

    def test_forget_reverts_to_untracked(self, tmp_path):
        path = tmp_path / "consent.json"
        store = ChannelConsentStore(path)
        store.set("C1", "declined")
        store.forget("C1")
        assert store.is_dormant("C1") is False
        assert ChannelConsentStore(path).status("C1") is None


# ---------------------------------------------------------------------------
# SlackAdapter integration
# ---------------------------------------------------------------------------


def make_adapter(tmp_path, gate=True, cjn=None, consent_prompt=None):
    config = PlatformConfig(
        enabled=True,
        token="***",
        channel_consent_gate=gate,
        channel_join_notification=cjn,
        channel_consent_prompt=consent_prompt,
    )
    a = SlackAdapter(config)
    a._app = MagicMock()
    client = AsyncMock()
    a._app.client = client
    a._bot_user_id = "U_BOT"
    a._running = True
    a._consent_store = ChannelConsentStore(tmp_path / "consent.json")
    a.handle_message = AsyncMock()
    return a, client


def join_event(user="U_BOT", channel="C_NEW", inviter="U_HUMAN"):
    return {
        "type": "member_joined_channel",
        "user": user,
        "channel": channel,
        "team": "T1",
        "inviter": inviter,
    }


def consent_click(action_id, channel="C_NEW", user_id="U_HUMAN"):
    body = {
        "channel": {"id": channel},
        "user": {"id": user_id, "name": "jake"},
        "message": {"ts": "123.456"},
    }
    action = {"action_id": action_id, "value": channel}
    return body, action


@pytest.mark.asyncio
async def test_gate_disabled_join_posts_no_prompt(tmp_path):
    a, client = make_adapter(tmp_path, gate=False)
    await a._handle_member_joined_channel(join_event())
    client.chat_postMessage.assert_not_awaited()
    assert a._consent_store.status("C_NEW") is None


@pytest.mark.asyncio
async def test_join_marks_pending_and_posts_prompt(tmp_path):
    a, client = make_adapter(tmp_path)
    await a._handle_member_joined_channel(join_event())
    assert a._consent_store.status("C_NEW") == "pending"
    client.chat_postMessage.assert_awaited_once()
    kwargs = client.chat_postMessage.await_args.kwargs
    assert kwargs["channel"] == "C_NEW"
    action_ids = {
        el["action_id"]
        for b in kwargs["blocks"]
        if b["type"] == "actions"
        for el in b["elements"]
    }
    assert action_ids == {"hermes_consent_activate", "hermes_consent_decline"}


@pytest.mark.asyncio
async def test_other_member_join_does_not_trigger_gate(tmp_path):
    a, client = make_adapter(tmp_path)
    await a._handle_member_joined_channel(join_event(user="U_SOMEONE"))
    client.chat_postMessage.assert_not_awaited()
    assert a._consent_store.status("C_NEW") is None


@pytest.mark.asyncio
async def test_default_prompt_mentions_data_visibility(tmp_path):
    a, client = make_adapter(tmp_path)
    await a._handle_member_joined_channel(join_event())
    text = client.chat_postMessage.await_args.kwargs["text"]
    assert "<@U_HUMAN>" in text  # inviter rendered
    assert "sensitive" in text  # data warning present
    assert "anyone" in text  # visibility warning present


@pytest.mark.asyncio
async def test_custom_consent_prompt_used(tmp_path):
    a, client = make_adapter(
        tmp_path,
        consent_prompt="Custom gate for {channel_ref}, invited by {inviter_ref}.",
    )
    await a._handle_member_joined_channel(join_event())
    text = client.chat_postMessage.await_args.kwargs["text"]
    assert text == "Custom gate for <#C_NEW>, invited by <@U_HUMAN>."


@pytest.mark.asyncio
async def test_invalid_consent_prompt_falls_back_to_default(tmp_path):
    a, client = make_adapter(
        tmp_path, consent_prompt="bad {nonexistent_placeholder}"
    )
    await a._handle_member_joined_channel(join_event())
    text = client.chat_postMessage.await_args.kwargs["text"]
    assert "sensitive" in text  # default used


@pytest.mark.asyncio
async def test_readd_to_approved_channel_reconfirms(tmp_path):
    """Removed + re-added → consent resets to pending and re-prompts."""
    a, client = make_adapter(tmp_path)
    a._consent_store.set("C_NEW", "approved", by_user_id="U_HUMAN")
    await a._handle_member_joined_channel(join_event())
    assert a._consent_store.status("C_NEW") == "pending"
    client.chat_postMessage.assert_awaited_once()


@pytest.mark.asyncio
async def test_duplicate_join_event_does_not_reset_approval(tmp_path):
    """Socket Mode redelivery of the SAME join event must not re-gate."""
    a, client = make_adapter(tmp_path)
    event = join_event()
    event["event_ts"] = "111.222"
    await a._handle_member_joined_channel(event)
    # Approved between delivery and redelivery
    a._consent_store.set("C_NEW", "approved", by_user_id="U_HUMAN")
    await a._handle_member_joined_channel(event)  # replay
    assert a._consent_store.status("C_NEW") == "approved"
    client.chat_postMessage.assert_awaited_once()  # no second prompt


def public_join_event(**kw):
    return join_event(**kw)


def private_join_event(**kw):
    return join_event(**kw)


def set_channel_privacy(client, is_private: bool):
    """Mock conversations.info — the source of truth for public/private.

    (The channel_type field on the event is 'C' for BOTH public and private
    channels, so the adapter must call conversations.info instead.)
    """
    client.conversations_info = AsyncMock(
        return_value={"ok": True, "channel": {"is_private": is_private}}
    )


@pytest.mark.asyncio
async def test_public_channels_gated_by_default(tmp_path):
    a, client = make_adapter(tmp_path)
    set_channel_privacy(client, is_private=False)
    await a._handle_member_joined_channel(public_join_event())
    assert a._consent_store.status("C_NEW") == "pending"
    client.chat_postMessage.assert_awaited_once()


@pytest.mark.asyncio
async def test_public_channels_skip_gate_when_configured(tmp_path):
    a, client = make_adapter(tmp_path)
    a.config.channel_consent_public_channels = False
    set_channel_privacy(client, is_private=False)
    await a._handle_member_joined_channel(public_join_event())
    assert a._consent_store.status("C_NEW") is None
    client.chat_postMessage.assert_not_awaited()
    assert a._is_channel_consent_blocked("C_NEW") is False


@pytest.mark.asyncio
async def test_private_channels_still_gated_when_public_skipped(tmp_path):
    a, client = make_adapter(tmp_path)
    a.config.channel_consent_public_channels = False
    set_channel_privacy(client, is_private=True)
    await a._handle_member_joined_channel(private_join_event())
    assert a._consent_store.status("C_NEW") == "pending"
    client.chat_postMessage.assert_awaited_once()


@pytest.mark.asyncio
async def test_privacy_lookup_failure_fails_closed(tmp_path):
    """conversations.info error → treat as private → gate applies."""
    a, client = make_adapter(tmp_path)
    a.config.channel_consent_public_channels = False
    client.conversations_info = AsyncMock(side_effect=RuntimeError("boom"))
    await a._handle_member_joined_channel(join_event())
    assert a._consent_store.status("C_NEW") == "pending"
    client.chat_postMessage.assert_awaited_once()


@pytest.mark.asyncio
async def test_public_skip_clears_stale_pending_record(tmp_path):
    """Config flipped to public_channels:false after a channel was gated."""
    a, client = make_adapter(tmp_path)
    a._consent_store.set("C_NEW", "pending")
    a.config.channel_consent_public_channels = False
    set_channel_privacy(client, is_private=False)
    await a._handle_member_joined_channel(public_join_event())
    assert a._consent_store.status("C_NEW") is None
    assert a._is_channel_consent_blocked("C_NEW") is False


# ---------------------------------------------------------------------------
# Consent audit trail (status-channel follow-ups)
# ---------------------------------------------------------------------------


def make_audit_adapter(tmp_path, cjn=None):
    from gateway.platforms.base import SendResult

    a, client = make_adapter(
        tmp_path, cjn=cjn or {"channel": "C_STATUS"}
    )
    a.send = AsyncMock(return_value=SendResult(success=True))
    a._is_interactive_user_authorized = MagicMock(return_value=True)
    return a, client


def sent_texts(a, target="C_STATUS"):
    return [
        c.args[1] for c in a.send.await_args_list if c.args[0] == target
    ]


@pytest.mark.asyncio
async def test_activate_posts_audit_to_status_channel(tmp_path):
    a, client = make_audit_adapter(tmp_path)
    a._consent_store.set("C_NEW", "pending")
    body, action = consent_click("hermes_consent_activate")
    await a._handle_consent_action(AsyncMock(), body, action)
    texts = sent_texts(a)
    assert len(texts) == 1
    assert "<#C_NEW>" in texts[0]
    assert "<@U_HUMAN>" in texts[0]
    assert "activated" in texts[0]


@pytest.mark.asyncio
async def test_decline_posts_audit_to_status_channel(tmp_path):
    a, client = make_audit_adapter(tmp_path)
    a._consent_store.set("C_NEW", "pending")
    body, action = consent_click("hermes_consent_decline")
    await a._handle_consent_action(AsyncMock(), body, action)
    texts = sent_texts(a)
    assert len(texts) == 1
    assert "declined" in texts[0]
    assert "<@U_HUMAN>" in texts[0]


@pytest.mark.asyncio
async def test_public_autoskip_posts_audit(tmp_path):
    a, client = make_audit_adapter(tmp_path)
    a.config.channel_consent_public_channels = False
    set_channel_privacy(client, is_private=False)
    await a._handle_member_joined_channel(public_join_event())
    texts = sent_texts(a)
    # join notification + auto-activated audit
    assert len(texts) == 2
    assert "automatically" in texts[1]
    assert "<#C_NEW>" in texts[1]


@pytest.mark.asyncio
async def test_audit_templates_overridable(tmp_path):
    a, client = make_audit_adapter(
        tmp_path,
        cjn={
            "channel": "C_STATUS",
            "activated": "AUDIT ON {channel_id} by {user_id}",
        },
    )
    a._consent_store.set("C_NEW", "pending")
    body, action = consent_click("hermes_consent_activate")
    await a._handle_consent_action(AsyncMock(), body, action)
    assert sent_texts(a) == ["AUDIT ON C_NEW by U_HUMAN"]


@pytest.mark.asyncio
async def test_invalid_audit_template_falls_back(tmp_path):
    a, client = make_audit_adapter(
        tmp_path,
        cjn={"channel": "C_STATUS", "declined": "bad {nope}"},
    )
    a._consent_store.set("C_NEW", "pending")
    body, action = consent_click("hermes_consent_decline")
    await a._handle_consent_action(AsyncMock(), body, action)
    texts = sent_texts(a)
    assert len(texts) == 1
    assert "declined" in texts[0]  # default template used


@pytest.mark.asyncio
async def test_no_audit_without_status_channel(tmp_path):
    """Gate works standalone — no channel_join_notification configured."""
    a, client = make_adapter(tmp_path, cjn=None)
    a.send = AsyncMock()
    a._is_interactive_user_authorized = MagicMock(return_value=True)
    a._consent_store.set("C_NEW", "pending")
    body, action = consent_click("hermes_consent_activate")
    await a._handle_consent_action(AsyncMock(), body, action)
    assert a._consent_store.status("C_NEW") == "approved"
    a.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_audit_send_failure_does_not_break_consent(tmp_path):
    from gateway.platforms.base import SendResult

    a, client = make_audit_adapter(tmp_path)
    a.send = AsyncMock(return_value=SendResult(success=False, error="boom"))
    a._consent_store.set("C_NEW", "pending")
    body, action = consent_click("hermes_consent_activate")
    await a._handle_consent_action(AsyncMock(), body, action)  # must not raise
    assert a._consent_store.status("C_NEW") == "approved"


# ---------------------------------------------------------------------------
# Removal logging (member_left_channel)
# ---------------------------------------------------------------------------


def leave_event(channel="C_NEW", actor="U_HUMAN", event_ts="99.1"):
    """channel_left / group_left — the app's OWN removal. Carries actor_id
    (who removed the bot); no 'user' field needed since the event is only
    delivered for the app itself.
    """
    return {
        "type": "channel_left",
        "channel": channel,
        "actor_id": actor,
        "event_ts": event_ts,
    }


@pytest.mark.asyncio
async def test_removal_posts_audit_and_clears_consent(tmp_path):
    a, client = make_audit_adapter(tmp_path)
    a._consent_store.set("C_NEW", "approved", by_user_id="U_HUMAN")
    await a._handle_bot_removed_from_channel(leave_event())
    assert a._consent_store.status("C_NEW") is None
    texts = sent_texts(a)
    assert len(texts) == 1
    assert "removed" in texts[0]
    assert "<#C_NEW>" in texts[0]
    assert "<@U_HUMAN>" in texts[0]  # actor named


@pytest.mark.asyncio
async def test_member_left_channel_is_noop(tmp_path):
    """Other members leaving must not trigger removal handling."""
    a, client = make_audit_adapter(tmp_path)
    a._consent_store.set("C_NEW", "approved", by_user_id="U_HUMAN")
    await a._handle_member_left_channel(
        {"type": "member_left_channel", "user": "U_SOMEONE", "channel": "C_NEW"}
    )
    assert a._consent_store.status("C_NEW") == "approved"
    a.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_removal_without_actor_renders_someone(tmp_path):
    a, client = make_audit_adapter(tmp_path)
    event = leave_event()
    del event["actor_id"]
    await a._handle_bot_removed_from_channel(event)
    texts = sent_texts(a)
    assert len(texts) == 1
    assert "someone" in texts[0]


@pytest.mark.asyncio
async def test_duplicate_leave_event_posts_once(tmp_path):
    a, client = make_audit_adapter(tmp_path)
    event = leave_event()
    await a._handle_bot_removed_from_channel(event)
    await a._handle_bot_removed_from_channel(event)  # replay
    assert len(sent_texts(a)) == 1


@pytest.mark.asyncio
async def test_removal_logged_without_consent_gate(tmp_path):
    """Removal audit is part of join notification, not gated on consent."""
    a, client = make_audit_adapter(tmp_path)
    a.config.channel_consent_gate = False
    await a._handle_bot_removed_from_channel(leave_event())
    texts = sent_texts(a)
    assert len(texts) == 1
    assert "removed" in texts[0]


@pytest.mark.asyncio
async def test_removal_template_overridable(tmp_path):
    a, client = make_audit_adapter(
        tmp_path,
        cjn={
            "channel": "C_STATUS",
            "removed": "GONE FROM {channel_id}, blame {user_id}",
        },
    )
    await a._handle_bot_removed_from_channel(leave_event())
    assert sent_texts(a) == ["GONE FROM C_NEW, blame U_HUMAN"]


@pytest.mark.asyncio
async def test_pending_channel_messages_are_dropped(tmp_path):
    a, _ = make_adapter(tmp_path)
    a._consent_store.set("C_NEW", "pending")
    await a._handle_slack_message(
        {
            "type": "message",
            "user": "U_HUMAN",
            "channel": "C_NEW",
            "channel_type": "channel",
            "team": "T1",
            "ts": "1.0",
            "text": "<@U_BOT> hello",
        }
    )
    a.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_untracked_channel_messages_flow(tmp_path):
    a, _ = make_adapter(tmp_path)
    await a._handle_slack_message(
        {
            "type": "message",
            "user": "U_HUMAN",
            "channel": "C_OLD",
            "channel_type": "channel",
            "team": "T1",
            "ts": "2.0",
            "text": "<@U_BOT> hello",
        }
    )
    a.handle_message.assert_awaited()


@pytest.mark.asyncio
async def test_dms_are_never_gated(tmp_path):
    a, _ = make_adapter(tmp_path)
    a._consent_store.set("D_DM", "pending")  # nonsensical, but must not gate
    await a._handle_slack_message(
        {
            "type": "message",
            "user": "U_HUMAN",
            "channel": "D_DM",
            "channel_type": "im",
            "team": "T1",
            "ts": "3.0",
            "text": "hello",
        }
    )
    a.handle_message.assert_awaited()


@pytest.mark.asyncio
async def test_activate_click_approves_and_unblocks(tmp_path):
    a, client = make_adapter(tmp_path)
    a._is_interactive_user_authorized = MagicMock(return_value=True)
    a._consent_store.set("C_NEW", "pending")

    body, action = consent_click("hermes_consent_activate")
    await a._handle_consent_action(AsyncMock(), body, action)

    assert a._consent_store.status("C_NEW") == "approved"
    assert a._is_channel_consent_blocked("C_NEW") is False
    client.chat_update.assert_awaited_once()  # prompt replaced, buttons gone


@pytest.mark.asyncio
async def test_decline_click_keeps_dormant(tmp_path):
    a, client = make_adapter(tmp_path)
    a._is_interactive_user_authorized = MagicMock(return_value=True)
    a._consent_store.set("C_NEW", "pending")

    body, action = consent_click("hermes_consent_decline")
    await a._handle_consent_action(AsyncMock(), body, action)

    assert a._consent_store.status("C_NEW") == "declined"
    assert a._is_channel_consent_blocked("C_NEW") is True


@pytest.mark.asyncio
async def test_unauthorized_click_is_ignored(tmp_path):
    a, client = make_adapter(tmp_path)
    a._is_interactive_user_authorized = MagicMock(return_value=False)
    a._consent_store.set("C_NEW", "pending")

    body, action = consent_click("hermes_consent_activate", user_id="U_RANDO")
    await a._handle_consent_action(AsyncMock(), body, action)

    assert a._consent_store.status("C_NEW") == "pending"
    client.chat_update.assert_not_awaited()


@pytest.mark.asyncio
async def test_join_notification_and_gate_both_fire(tmp_path):
    from gateway.platforms.base import SendResult

    a, client = make_adapter(
        tmp_path, cjn={"channel": "C_STATUS"}
    )
    a.send = AsyncMock(return_value=SendResult(success=True))
    await a._handle_member_joined_channel(join_event())
    # Status ping sent
    a.send.assert_awaited_once()
    assert a.send.await_args.args[0] == "C_STATUS"
    # Consent prompt posted into the joined channel
    client.chat_postMessage.assert_awaited_once()
    assert a._consent_store.status("C_NEW") == "pending"
