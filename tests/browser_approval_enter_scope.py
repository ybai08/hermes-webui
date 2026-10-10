#!/usr/bin/env python3
"""
Headless browser gate: while an approval card is up, Enter approves only when
nothing else has the keyboard (#8130).

WHY THIS EXISTS
  The document-level shortcut "Enter = Allow once" ran whenever the card was
  visible and focus was outside a text field. It also cancelled the key's own
  action. So Enter on any focused button approved the pending tool action and
  the button did nothing: the conversation's ⋮ trigger, a ⋮ menu item, a rail
  tab, and the card's own Deny, Allow session and Always buttons.

WHAT IT CHECKS, with the real card shown by showApprovalCard() and every answer
recorded instead of sent
  outside the card: Enter on a rail tab, on a conversation's ⋮ trigger, on a
    ⋮ menu item and on a row of the "Move to project" picker does what that
    control does and approves nothing;
  inside the card: Enter on Allow once, Allow session, Always and Deny gives
    that button's answer, once; on the collapse button it collapses the card;
    on "Skip all" it asks for that; none of these also allows once;
  the shortcut itself: with nothing focused, Enter allows once; the card takes
    focus on Allow once when it appears, so Enter right after it shows allows
    once; Shift+Enter and Ctrl+Enter approve nothing; in the composer Enter
    approves nothing; with no card up Enter approves nothing;
  every kind of element that owns Enter, one probe element each, added to the
    page for the purpose (a link, a summary, a text field, each ARIA role the
    rule names, something in the tab order): Enter on it approves nothing. An
    element that takes only programmatic focus (tabindex="-1", no role) is not
    one of them: Enter there allows once, as with nothing focused.

SCOPE
  Agent-free, like tests/browser_smoke.py: the real server.py on an ephemeral
  port with isolated temp state. No agent runs, so no approval is really
  pending: the card is shown through the app's own showApprovalCard(), and
  respondApproval() and toggleYoloFromApproval() are replaced by recorders.
  This proves which key reaches which answer in the page, in Chromium. It does
  not prove what the server does with an answer.

USAGE
  python tests/browser_approval_enter_scope.py
  (Requires: playwright + chromium.)

EXIT CODES
  0 — every case passed
  1 — a check failed (regression)
  2 — environment/setup failure (server didn't boot, playwright missing, etc.)
"""
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

PORT = int(os.getenv("APPROVAL_ENTER_PORT", "8796"))
BASE = f"http://127.0.0.1:{PORT}"
WAIT_MS = 5000
# The card focuses "Allow once" 50 ms after it appears; let that happen before
# a case moves focus elsewhere, or it would take the focus back mid-case.
CARD_FOCUS_MS = 250

SEED_JS = """async () => {
  const response = await fetch('/api/session/import', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({title: 'Alpha conversation', messages: [
      {role: 'user', content: 'hello'}, {role: 'assistant', content: 'hello back'},
    ]}),
  });
  const data = await response.json();
  if (!response.ok) throw new Error('import failed: ' + JSON.stringify(data));
  const made = await fetch('/api/projects/create', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({name: 'Research', color: '#7cb9ff'}),
  });
  const project = await made.json();
  if (!made.ok) throw new Error('project failed: ' + JSON.stringify(project));
  await renderSessionList();
  if (!S.session) await newSession();
  return {sid: data.session.session_id, project: project.project.project_id};
}"""

RECORD_JS = """() => {
  window.__answers = [];
  window.respondApproval = (choice) => { window.__answers.push(choice); };
  window.toggleYoloFromApproval = () => { window.__answers.push('skip-all'); };
}"""

SHOW_CARD_JS = """() => {
  window.__answers.length = 0;
  showApprovalCard({
    description: 'gate approval', command: 'echo gate',
    approval_id: 'gate-' + Date.now() + '-' + Math.random(),
    _session_id: S.session.session_id,
  }, 1);
  return $('approvalCard').classList.contains('visible');
}"""

HIDE_CARD_JS = "() => { hideApprovalCard(true); }"

STATE_JS = """() => ({
  answers: window.__answers.slice(),
  panel: (document.querySelector('.rail-btn.nav-tab.active') || {dataset: {}}).dataset.panel || null,
  menu: !!document.querySelector('.session-action-menu'),
  picker: !!document.querySelector('.project-picker'),
  collapsed: $('approvalCard').classList.contains('collapsed'),
  probeClicks: window.__probeClicks || 0,
  visible: $('approvalCard').classList.contains('visible'),
})"""

FOCUS_SELECTOR_JS = """(selector) => {
  const el = document.querySelector(selector);
  if (!el) return 'no element for ' + selector;
  el.focus();
  return document.activeElement === el ? null : 'could not focus ' + selector;
}"""

FOCUS_PICKER_ROW_JS = """(name) => {
  const row = Array.from(document.querySelectorAll('.project-picker .project-picker-item'))
    .find(item => item.textContent.trim() === name);
  if (!row) return 'the picker has no row ' + name;
  row.focus();
  return document.activeElement === row ? null : "could not focus the picker's row";
}"""

FOCUS_MENU_ITEM_JS = """() => {
  const label = t('session_move_project');
  const item = Array.from(document.querySelectorAll('.session-action-menu .session-action-opt'))
    .find(opt => opt.textContent.trim() === label);
  if (!item) return "the ⋮ menu has no 'Move to project' item";
  item.focus();
  return document.activeElement === item ? null : "could not focus the ⋮ menu's item";
}"""

BLUR_JS = "() => { if (document.activeElement) document.activeElement.blur(); return null; }"

# One element per clause of the rule, and whether it owns Enter. The roles get
# tabindex="-1" so that only the role can be what makes them owners.
PROBES = (
    ("a button out of the tab order", '<button type="button" tabindex="-1">b</button>', True),
    ("a link", '<a href="#gate">a</a>', True),
    ("an input", '<input type="text">', True),
    ("a select", "<select><option>one</option></select>", True),
    ("a textarea", "<textarea></textarea>", True),
    ("a summary", "<summary>s</summary>", True),
    ("an editable element", '<div contenteditable="true">e</div>', True),
    ("role=button", '<div role="button" tabindex="-1">r</div>', True),
    ("role=link", '<div role="link" tabindex="-1">r</div>', True),
    ("role=menuitem", '<div role="menuitem" tabindex="-1">r</div>', True),
    ("role=menuitemradio", '<div role="menuitemradio" tabindex="-1">r</div>', True),
    ("role=menuitemcheckbox", '<div role="menuitemcheckbox" tabindex="-1">r</div>', True),
    ("role=option", '<div role="option" tabindex="-1">r</div>', True),
    ("role=tab", '<div role="tab" tabindex="-1">r</div>', True),
    ("an element in the tab order", '<div tabindex="0">t</div>', True),
    ("text inside an element in the tab order", '<div tabindex="0"><span tabindex="-1">t</span></div>', True),
    ("an element that takes only programmatic focus", '<div tabindex="-1">p</div>', False),
    ("the same, marked not editable", '<div tabindex="-1" contenteditable="false">p</div>', False),
)

# Put the probe in the page, outside the card, and focus its innermost element.
PROBE_JS = """(html) => {
  const old = document.getElementById('gateProbe');
  if (old) old.remove();
  const box = document.createElement('div');
  box.id = 'gateProbe';
  box.style.cssText = 'position:fixed;left:0;top:0;z-index:99999';
  box.innerHTML = html.startsWith('<summary') ? '<details>' + html + 'body</details>' : html;
  // The click the browser makes of Enter on a button, a link or a summary. A
  // link must not really navigate: that would reload the page under the gate.
  window.__probeClicks = 0;
  box.addEventListener('click', (event) => {
    window.__probeClicks += 1;
    if (event.target.closest('a')) event.preventDefault();
  });
  document.body.appendChild(box);
  let el = box;
  while (el.firstElementChild) el = el.firstElementChild;
  if (el.tagName === 'OPTION') el = el.parentElement;
  el.focus();
  return document.activeElement === el ? null : 'the probe could not take focus';
}"""

# Inside the card: the button, and the one answer Enter on it must give.
CARD_BUTTONS = (
    ("Allow once", "#approvalBtnOnce", ["once"]),
    ("Allow session", "#approvalBtnSession", ["session"]),
    ("Always", "#approvalBtnAlways", ["always"]),
    ("Deny", "#approvalBtnDeny", ["deny"]),
    ("Skip all", "#approvalSkipAll", ["skip-all"]),
)


def _wait_for_health(timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(BASE + "/health", timeout=2) as r:
                if r.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(0.5)
    return False


def _wait_until(page, expression, timeout_ms=WAIT_MS):
    """Poll ``expression`` with page.evaluate. The app's CSP has no 'unsafe-eval',
    which Playwright's interval-polled wait_for_function needs."""
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        if page.evaluate(expression):
            return True
        page.wait_for_timeout(100)
    return False


def _show_card(page):
    """A fresh card, its own focus move done. Returns a failure line or None."""
    page.evaluate(HIDE_CARD_JS)
    if not page.evaluate(SHOW_CARD_JS):
        return "the approval card did not show"
    page.wait_for_timeout(CARD_FOCUS_MS)
    return None


def _press(page, name, focus_js, focus_arg, key, *, card=True):
    """Show the card (or not), put focus where the case wants it, press ``key``.
    Returns (state, failure line or None)."""
    if card:
        problem = _show_card(page)
    else:
        page.evaluate(HIDE_CARD_JS)
        page.evaluate("() => { window.__answers.length = 0; }")
        problem = None
    if not problem and focus_js:
        problem = page.evaluate(focus_js, focus_arg) if focus_arg else page.evaluate(focus_js)
    if problem:
        return None, f"  [{name}] setup: {problem}"
    page.keyboard.press(key)
    page.wait_for_timeout(300)
    return page.evaluate(STATE_JS), None


def _check(page, seed, moves):
    failures = []
    sid = seed["sid"]

    def expect(name, state, problem, **wanted):
        if problem:
            failures.append(problem)
            return
        for field, value in wanted.items():
            if state[field] != value:
                failures.append(f"  [{name}] {field} is {state[field]!r}, expected {value!r}")

    # ── outside the card: the control keeps its own Enter ──
    name = "rail tab"
    state, problem = _press(page, name, FOCUS_SELECTOR_JS, '.rail-btn[data-panel="tasks"]', "Enter")
    expect(name, state, problem, answers=[], panel="tasks")
    page.evaluate("() => switchPanel('chat')")
    page.wait_for_timeout(300)

    trigger = f'.session-item[data-sid="{sid}"] .session-actions-trigger'
    name = "⋮ trigger"
    state, problem = _press(page, name, FOCUS_SELECTOR_JS, trigger, "Enter")
    expect(name, state, problem, answers=[], menu=True)

    # The menu the trigger just opened is still there; a new card for the item.
    name = "⋮ menu item"
    if state and state["menu"]:
        state, problem = _press(page, name, FOCUS_MENU_ITEM_JS, None, "Enter")
        expect(name, state, problem, answers=[], picker=True)
    else:
        # Opened with a click instead, so this case does not depend on the last.
        page.evaluate(HIDE_CARD_JS)
        page.click(trigger)
        if not _wait_until(page, "!!document.querySelector('.session-action-menu')", 1500):
            failures.append(f"  [{name}] setup: the ⋮ menu did not open by a click")
        else:
            state, problem = _press(page, name, FOCUS_MENU_ITEM_JS, None, "Enter")
            expect(name, state, problem, answers=[], picker=True)
    # The picker the item just opened: Enter on a project moves the conversation.
    name = "Move to project row"
    if not (state and state["picker"]):
        # Opened with the mouse instead, so this case does not depend on the last.
        page.evaluate(HIDE_CARD_JS)
        page.keyboard.press("Escape")
        page.click(trigger)
        _wait_until(page, "!!document.querySelector('.session-action-menu')", 1500)
        page.evaluate(FOCUS_MENU_ITEM_JS)
        page.evaluate("() => document.activeElement.click()")
    if not _wait_until(page, "!!document.querySelector('.project-picker')", 1500):
        failures.append(f"  [{name}] setup: the picker did not open")
    else:
        moves.clear()
        state, problem = _press(page, name, FOCUS_PICKER_ROW_JS, "Research", "Enter")
        expect(name, state, problem, answers=[], picker=False)
        if not problem and moves != [{"session_id": sid, "project_id": seed["project"]}]:
            failures.append(f"  [{name}] sent {moves}, expected one move to the project")
    page.keyboard.press("Escape")
    page.mouse.click(700, 300)
    page.wait_for_timeout(200)

    # ── inside the card: each button gives its own answer, once ──
    for label, selector, answers in CARD_BUTTONS:
        name = f"card: {label}"
        state, problem = _press(page, name, FOCUS_SELECTOR_JS, selector, "Enter")
        expect(name, state, problem, answers=answers)

    name = "card: collapse"
    state, problem = _press(page, name, FOCUS_SELECTOR_JS, "#approvalCollapse", "Enter")
    expect(name, state, problem, answers=[], collapsed=True)

    # ── the shortcut itself ──
    name = "nothing focused"
    state, problem = _press(page, name, BLUR_JS, None, "Enter")
    expect(name, state, problem, answers=["once"])

    name = "right after the card appears"
    state, problem = _press(page, name, None, None, "Enter")
    expect(name, state, problem, answers=["once"])

    for key in ("Shift+Enter", "Control+Enter"):
        name = f"nothing focused, {key}"
        state, problem = _press(page, name, BLUR_JS, None, key)
        expect(name, state, problem, answers=[])

    name = "composer"
    state, problem = _press(page, name, FOCUS_SELECTOR_JS, "#msg", "Enter")
    expect(name, state, problem, answers=[])

    # ── every kind of element the rule names ──
    for what, html, owns in PROBES:
        name = f"probe: {what}"
        state, problem = _press(page, name, PROBE_JS, html, "Enter")
        expect(name, state, problem, answers=[] if owns else ["once"])
        if what in ("a button out of the tab order", "a link", "a summary") and state:
            # Its own Enter really happened: not approved because not cancelled.
            expect(name, state, None, probeClicks=1)
    page.evaluate("() => { const p = document.getElementById('gateProbe'); if (p) p.remove(); }")

    name = "no card"
    state, problem = _press(page, name, BLUR_JS, None, "Enter", card=False)
    expect(name, state, problem, answers=[], visible=False)

    return failures


def main():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("SKIP: playwright not installed", file=sys.stderr)
        return 2

    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    server_py = os.path.join(repo_root, "server.py")
    state_dir = tempfile.mkdtemp(prefix="hermes-approval-enter-")
    env = os.environ.copy()
    for k in list(env):
        if k.endswith("_API_KEY"):
            env.pop(k, None)
    env.update({
        "HERMES_WEBUI_PORT": str(PORT),
        "HERMES_WEBUI_HOST": "127.0.0.1",
        "HERMES_WEBUI_STATE_DIR": state_dir,
        "HERMES_HOME": state_dir,
        "HERMES_BASE_HOME": state_dir,
        "HERMES_WEBUI_SKIP_ONBOARDING": "1",
        "HERMES_WEBUI_AGENT_DIR": os.path.join(state_dir, "no-agent"),
    })

    log = open(os.path.join(state_dir, "server.log"), "w")
    proc = subprocess.Popen(
        [sys.executable, server_py], cwd=repo_root, env=env,
        stdout=log, stderr=subprocess.STDOUT,
        **({"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}),
    )
    try:
        if not _wait_for_health(timeout=30):
            print("SETUP FAIL: server did not become healthy in 30s", file=sys.stderr)
            log.flush()
            with open(os.path.join(state_dir, "server.log")) as f:
                print(f.read()[-2000:], file=sys.stderr)
            return 2

        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"]
            )
            ctx = browser.new_context(base_url=BASE, viewport={"width": 1440, "height": 900})
            page = ctx.new_page()
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto("/", wait_until="domcontentloaded")
            page.wait_for_selector("#msg", timeout=15000)
            ready = (
                "typeof S !== 'undefined' && typeof renderSessionList === 'function'"
                " && typeof showApprovalCard === 'function'"
            )
            if not _wait_until(page, ready, 15000):
                print("SETUP FAIL: the app did not initialize", file=sys.stderr)
                return 2
            page.wait_for_timeout(1000)
            seed = page.evaluate(SEED_JS)
            page.evaluate(RECORD_JS)
            moves = []
            page.on(
                "request",
                lambda r: moves.append(r.post_data_json)
                if r.method == "POST" and urlsplit(r.url).path == "/api/session/move" else None,
            )
            page.wait_for_timeout(600)
            failures = _check(page, seed, moves)
            failures.extend(f"  pageerror: {err}" for err in errors)
            browser.close()

        if failures:
            print("\nAPPROVAL ENTER SCOPE FAILED:", file=sys.stderr)
            print("\n".join(failures), file=sys.stderr)
            return 1
        print("OK  outside the card, inside the card, and the shortcut itself")
        print("\nAPPROVAL ENTER SCOPE PASSED")
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())
