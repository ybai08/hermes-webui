"""#8130 — Enter approves a pending tool action only when nothing else has the keyboard.

While the approval card was visible, the document-level shortcut in
``static/boot.js`` took Enter from every focused element except a text field,
cancelled the key's own action and allowed the action once. Enter on a rail tab,
on a conversation's ⋮ trigger, on a ⋮ menu item, and on the card's own Deny,
Allow session and Always buttons all approved.

The shortcut's block is run here in node with a stand-in for the page, so the
decision is exercised and not only read: what it does for a focused control, for
nothing focused, with a modifier, and with no card up. Which real elements count
as a control is a CSS selector, and node has no selector engine: that half, and
every case end to end, is tests/browser_approval_enter_scope.py in Chromium.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
BOOT_JS = (REPO / "static" / "boot.js").read_text(encoding="utf-8")
GATE = "tests/browser_approval_enter_scope.py"

NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")


def _between(source: str, start: str, end: str) -> str:
    begin = source.index(start)
    return source[begin:source.index(end, begin)]


HELPER = _between(BOOT_JS, "const _ENTER_OWNER_SELECTOR=", "// B14: Cmd/Ctrl+K")
SHORTCUT = _between(
    BOOT_JS, "  // Enter while the approval card is visible", "  if((e.metaKey||e.ctrlKey)&&e.key==='k'){"
)

# The page, as far as the shortcut can see it: a card that is visible or not, a
# focused element that is a control or not, and a recorder for the answer.
PAGE = r"""
const state = JSON.parse(process.argv[1]);
const answers = [];
const card = {classList: {contains: name => name === 'visible' && state.visible}};
const $ = id => (id === 'approvalCard' && state.card !== false ? card : null);
const asked = [];
const focused = state.focus === 'nothing'
  ? {tagName: 'BODY', closest: selector => { asked.push(selector); return null; }}
  : {tagName: 'BUTTON', closest: selector => { asked.push(selector); return {}; }};
const document = {activeElement: state.focus === 'null' ? null : focused};
function respondApproval(choice){ answers.push(choice); }
const e = Object.assign({key: 'Enter', metaKey: false, ctrlKey: false, shiftKey: false, prevented: false,
  preventDefault(){ this.prevented = true; }}, state.event || {});
"""


def _press(**state):
    script = PAGE + HELPER + "function shortcut(e){\n" + SHORTCUT + "\n}\nshortcut(e);\n" + (
        "console.log(JSON.stringify({answers, prevented: e.prevented, asked,"
        " selector: _ENTER_OWNER_SELECTOR}));"
    )
    result = subprocess.run(
        [NODE, "-e", script, json.dumps(state)], check=True, capture_output=True, text=True
    )
    return json.loads(result.stdout)


@needs_node
class TestTheShortcut:
    def test_nothing_focused_allows_once_and_takes_the_key(self):
        out = _press(visible=True, focus="nothing")

        assert out["answers"] == ["once"]
        assert out["prevented"] is True

    def test_no_active_element_at_all_allows_once(self):
        out = _press(visible=True, focus="null")

        assert out["answers"] == ["once"]
        assert out["prevented"] is True

    def test_a_focused_control_keeps_its_enter(self):
        """Not approved, and not cancelled either: the control's own Enter
        still has to happen."""
        out = _press(visible=True, focus="control")

        assert out["answers"] == []
        assert out["prevented"] is False

    def test_the_focused_element_is_asked_with_the_owner_selector(self):
        out = _press(visible=True, focus="control")

        assert out["asked"] == [out["selector"]]

    @pytest.mark.parametrize("focus", ["nothing", "control"])
    def test_no_visible_card_means_no_answer(self, focus):
        out = _press(visible=False, focus=focus)

        assert out["answers"] == []
        assert out["prevented"] is False

    def test_no_card_element_means_no_answer(self):
        out = _press(visible=True, card=False, focus="nothing")

        assert out["answers"] == []
        assert out["prevented"] is False

    @pytest.mark.parametrize("modifier", ["shiftKey", "ctrlKey", "metaKey"])
    def test_a_modifier_means_no_answer(self, modifier):
        out = _press(visible=True, focus="nothing", event={modifier: True})

        assert out["answers"] == []
        assert out["prevented"] is False

    def test_another_key_means_no_answer(self):
        out = _press(visible=True, focus="nothing", event={"key": " "})

        assert out["answers"] == []
        assert out["prevented"] is False


OWNERS = [
    "button",
    "a[href]",
    "input",
    "select",
    "textarea",
    "summary",
    '[contenteditable]:not([contenteditable="false"])',
    '[role="button"]',
    '[role="link"]',
    '[role="menuitem"]',
    '[role="menuitemradio"]',
    '[role="menuitemcheckbox"]',
    '[role="option"]',
    '[role="tab"]',
    '[tabindex]:not([tabindex="-1"])',
]


def _selector() -> list[str]:
    line = next(
        line for line in BOOT_JS.splitlines() if line.startswith("const _ENTER_OWNER_SELECTOR=")
    )
    return line[len("const _ENTER_OWNER_SELECTOR='"):-len("';")].split(",")


class TestWhatOwnsEnter:
    def test_the_selector_is_exactly_these(self):
        """The text fields the old check exempted are still here, and so is
        every kind of control the app builds. An element that takes only
        programmatic focus (tabindex="-1") is not: nothing is typed into it and
        it does nothing on Enter."""
        assert _selector() == OWNERS

    def test_the_old_tag_list_is_gone(self):
        assert "tag!=='TEXTAREA'&&tag!=='INPUT'&&tag!=='SELECT'" not in BOOT_JS

    def test_the_shortcut_is_the_only_caller_of_allow_once_in_boot_js(self):
        assert BOOT_JS.count("respondApproval('once')") == 1
        assert "respondApproval('once')" in SHORTCUT


def test_the_browser_gate_runs_in_ci():
    workflow = (REPO / ".github" / "workflows" / "browser-smoke.yml").read_text(encoding="utf-8")

    assert f"run: python {GATE}" in workflow
    assert (REPO / GATE).is_file()
