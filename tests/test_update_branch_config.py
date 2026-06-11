"""Tests for _resolve_update_branch — the ``hermes update`` branch default.

Resolution order: --branch flag > updates.branch in config.yaml > "main".
"""
from types import SimpleNamespace
from unittest.mock import patch

from hermes_cli.main import _resolve_update_branch


def args(branch=None):
    return SimpleNamespace(branch=branch)


def with_config(cfg):
    return patch("hermes_cli.config.load_config", return_value=cfg)


class TestResolveUpdateBranch:
    def test_defaults_to_main(self):
        with with_config({}):
            assert _resolve_update_branch(args()) == "main"

    def test_explicit_flag_wins(self):
        with with_config({"updates": {"branch": "team-agent"}}):
            assert _resolve_update_branch(args("hotfix")) == "hotfix"

    def test_config_branch_used_when_no_flag(self):
        with with_config({"updates": {"branch": "team-agent"}}):
            assert _resolve_update_branch(args()) == "team-agent"

    def test_blank_flag_falls_through_to_config(self):
        with with_config({"updates": {"branch": "team-agent"}}):
            assert _resolve_update_branch(args("   ")) == "team-agent"

    def test_blank_config_falls_through_to_main(self):
        with with_config({"updates": {"branch": "  "}}):
            assert _resolve_update_branch(args()) == "main"

    def test_missing_updates_section(self):
        with with_config({"other": True}):
            assert _resolve_update_branch(args()) == "main"

    def test_config_error_never_breaks_updates(self):
        with patch(
            "hermes_cli.config.load_config",
            side_effect=RuntimeError("corrupt config"),
        ):
            assert _resolve_update_branch(args()) == "main"

    def test_none_updates_section(self):
        with with_config({"updates": None}):
            assert _resolve_update_branch(args()) == "main"
