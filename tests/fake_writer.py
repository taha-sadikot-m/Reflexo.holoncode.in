"""A deterministic stand-in for the `messages` role (BETA_13 §6) in tests that do not test the writing itself.

It reads the facts and buttons from the writer's prompt and returns a message that states every fact as given and
labels every button with its id, so the real check passes and the real compose path runs end to end.
"""

from __future__ import annotations

import json
import re

import reflexo.agent.api as agent

MESSAGES_MODEL = "gemini-2.5-flash-lite"  # REFLEXO_MODEL_MESSAGES in tests/conftest.py: apart from the desks' primary


def _section(prompt: str, title: str) -> list[str]:
    match = re.search(rf"# {title}[^\n]*\n(.*?)(?:\n# |\Z)", prompt, re.S)
    return [ln[2:] for ln in (match.group(1).splitlines() if match else []) if ln.startswith("- ")]


def writer(a: agent.Attempt) -> agent.FakeReply:
    prompt = next((m.content or "" for m in a.messages if m.role == "user"), "")
    if "# PURPOSE" not in prompt:  # another role on the same model: the provider's default echo
        return agent.FakeReply(text=f"echo: {prompt}")
    facts = [ln.split(": ", 1) for ln in _section(prompt, "FACTS")]
    text = "\n".join(f"{k}: {v}" for k, v in facts if len([k, v]) == 2)
    buttons = [{"id": ln.split(":", 1)[0], "label": ln.split(":", 1)[0][:20]} for ln in _section(prompt, "BUTTONS")]
    return agent.FakeReply(text=json.dumps({"text": text, "buttons": buttons}))


def install(fake: agent.FakeProvider) -> agent.FakeProvider:
    fake.script(MESSAGES_MODEL, writer)
    return fake
