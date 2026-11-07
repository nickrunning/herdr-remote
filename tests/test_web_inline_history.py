"""Tests for the conversation arriving above the terminal output, in the session's own view.

An agent pane usually has one screen of output and nothing behind it, so scrolling up used to stop
there. Arriving at the top now brings in the transcript. What can go wrong is all about geometry
and timing -- a page landing under the reader, a 3s tick moving them, a stale read left in the DOM
-- so everything here is measured in a browser against a stand-in socket.

Skipped, not failed, when playwright or a chromium build is missing.
"""
import os
from pathlib import Path
import unittest


PAGE = (Path(__file__).resolve().parents[1] / "web" / "index.html").as_uri()

CHROME_CANDIDATES = [
    os.environ.get("HERDR_TEST_CHROME", ""),
    os.path.expanduser("~/.cache/ms-playwright/chromium-1223/chrome-linux64/chrome"),
    "/usr/bin/chromium",
    "/usr/bin/google-chrome",
]

PHONE = {"width": 390, "height": 844}
DESKTOP = {"width": 1280, "height": 1000}


def _chrome():
    for path in CHROME_CANDIDATES:
        if path and os.path.exists(path):
            return path
    return None


try:  # pragma: no cover - environment probe
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover
    sync_playwright = None

_shared = {}


def setUpModule():  # noqa: N802 - unittest's own name
    if sync_playwright is None or _chrome() is None:
        return
    _shared["playwright"] = sync_playwright().start()
    _shared["browser"] = _shared["playwright"].chromium.launch(executable_path=_chrome())


def tearDownModule():  # noqa: N802 - unittest's own name
    if "browser" in _shared:
        _shared["browser"].close()
        _shared["playwright"].stop()
    _shared.clear()


def _pane(pane_id, **extra):
    return {"pane_id": pane_id, "label": "", "cwd": "/work/api", "project": "api", "host": "local",
            "remote": None, "workspace_id": "wB", "tab_id": "wB:t1", "focused": False,
            "scrollback": 0, "viewport_rows": 40, **extra}


SNAPSHOT = {
    "type": "agents",
    "agents": [
        _pane("wB:pA", agent="claude", status="idle", title="", has_session=False),
        _pane("wB:pB", agent="claude", status="idle", title="", has_session=True),
        _pane("wB:pC", agent="agy", status="idle", title="", has_session=False, scrollback=500),
        _pane("wB:pD", agent="agy", status="idle", title="", has_session=False),
    ],
    "panes": [_pane("wB:pS", scrollback=900)],
    "spaces": {"workspaces": [{"workspace_id": "wB", "label": "api", "number": 1, "focused": True,
                               "tab_count": 1, "pane_count": 5, "host": "local"}],
               "tabs": [{"tab_id": "wB:t1", "workspace_id": "wB", "label": "1", "number": 1,
                         "focused": True, "pane_count": 5, "host": "local"}]},
}

BODY = "\n".join(f"output line {i}" for i in range(120))


def turns(start, count):
    return [{"uuid": f"t{i}", "role": "user" if i % 2 else "assistant", "text": f"turn number {i}",
             "ts": "2026-08-21T00:00:00Z"} for i in range(start, start + count)]


@unittest.skipIf(sync_playwright is None, "playwright is not installed")
@unittest.skipIf(_chrome() is None, "no chromium build available")
class _Base(unittest.TestCase):
    viewport = PHONE

    @classmethod
    def setUpClass(cls):
        cls.page = _shared["browser"].new_page(viewport=cls.viewport)
        cls.page.goto(PAGE)

    @classmethod
    def tearDownClass(cls):
        cls.page.close()

    def setUp(self):
        self.page.evaluate("""s => {
          activeWorkspace = null; activeTab = null; activePane = null;
          handleMessage(s);
          ws = {readyState: 1, send: p => window.__sent.push(JSON.parse(p))};
          window.__sent = [];
        }""", SNAPSHOT)

    def tearDown(self):
        self.page.evaluate("getSelection().removeAllRanges()")
        self.page.evaluate("() => { if (activePane) hideTerminal(); }")

    def open(self, pane_id, body=BODY):
        self.page.evaluate("""([id, body]) => {
          openTerminal(id);
          clearInterval(refreshInterval);
          handleMessage({type: 'pane_content', pane_id: id, content: body});
          // What the real scroll event would have recorded, had it fired yet: it is asynchronous.
          mirrorScrollTop = document.getElementById('termScroll').scrollTop;
          window.__sent = [];
        }""", [pane_id, body])

    def scroll_to(self, top):
        """A real scroll, delivered synchronously: the handler is what is under test."""
        self.page.evaluate("""top => {
          const el = document.getElementById('termScroll');
          el.scrollTop = top;
          el.dispatchEvent(new Event('scroll'));
        }""", top)

    def to_top(self):
        self.scroll_to(0)

    def to_bottom(self):
        self.scroll_to(10 ** 7)

    def sent(self, kind):
        return [m for m in self.page.evaluate("window.__sent") if m["type"] == kind]

    def answer(self, pane_id, **body):
        self.page.evaluate("""([id, body]) => handleMessage(
          {type: 'history', pane_id: id, messages: [], total: 0, has_more: false, title: '',
           file_truncated: false, unavailable: null, ...body})""", [pane_id, body])

    def inline_text(self):
        return self.page.eval_on_selector("#termInlineHistory", "e => e.textContent")

    def inline_visible(self):
        return self.page.eval_on_selector(
            "#termInlineHistory", "e => getComputedStyle(e).display !== 'none'")

    def top(self):
        return self.page.eval_on_selector("#termScroll", "e => e.scrollTop")

    def line_y(self, selector_text):
        """Where a piece of text sits on the screen, which is what the reader's eye is holding."""
        return self.page.evaluate("""text => {
          const hit = [...document.querySelectorAll('#termInlineHistory .msg-text, #termContent .term-line')]
            .find(e => e.textContent.includes(text));
          return hit ? hit.getBoundingClientRect().top : null;
        }""", selector_text)


class ArrivingAtTheTopTests(_Base):
    def test_arriving_at_the_top_asks_for_the_conversation(self):
        self.open("wB:pA")
        self.to_top()
        requests = self.sent("get_history")
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["pane_id"], "wB:pA")
        self.assertNotIn("before", requests[0])
        self.assertEqual(self.sent("read_pane"), [])

    def test_the_turns_land_above_the_output_without_moving_it(self):
        self.open("wB:pA")
        self.to_top()
        before = self.line_y("output line 0")
        self.answer("wB:pA", messages=turns(0, 30), total=30)
        self.assertIn("turn number 29", self.inline_text())
        self.assertAlmostEqual(self.line_y("output line 0"), before, delta=1)
        self.assertGreater(self.top(), 0)
        self.assertTrue(self.page.evaluate("""() => {
          const inline = document.getElementById('termInlineHistory').getBoundingClientRect();
          const term = document.getElementById('termContent').getBoundingClientRect();
          return inline.bottom <= term.top + 1;
        }"""))

    def test_the_seam_between_transcript_and_terminal_is_marked(self):
        self.open("wB:pA")
        self.to_top()
        self.answer("wB:pA", messages=turns(0, 3), total=3)
        self.assertEqual(
            self.page.eval_on_selector("#termInlineHistory .hist-live", "e => e.textContent"),
            "Live terminal")

    def test_a_pane_herdr_names_a_session_for_works_the_same(self):
        self.open("wB:pB")
        self.to_top()
        self.assertEqual(len(self.sent("get_history")), 1)

    def test_scrolling_on_up_pages_back_and_holds_the_reader_in_place(self):
        self.open("wB:pA")
        self.to_top()
        self.answer("wB:pA", messages=turns(100, 40), total=140, has_more=True)
        self.to_top()
        older = self.sent("get_history")[-1]
        self.assertEqual(older["before"], "t100")
        before = self.line_y("turn number 100")
        self.answer("wB:pA", messages=turns(60, 40), total=140, has_more=True)
        self.assertIn("turn number 60", self.inline_text())
        self.assertAlmostEqual(self.line_y("turn number 100"), before, delta=1)

    def test_the_start_of_the_conversation_stops_asking(self):
        self.open("wB:pA")
        self.to_top()
        self.answer("wB:pA", messages=turns(0, 40), total=40)
        self.to_top()
        self.assertEqual(len(self.sent("get_history")), 1)
        self.assertIn("Start of the conversation.", self.inline_text())

    def test_a_terminal_still_pages_its_own_ring(self):
        self.open("wB:pS")
        self.to_top()
        self.assertEqual(self.sent("get_history"), [])
        self.assertEqual([m["lines"] for m in self.sent("read_pane")], [600])
        self.assertFalse(self.inline_visible())


class NoTranscriptTests(_Base):
    def test_an_agent_with_a_ring_falls_back_to_paging_it(self):
        self.open("wB:pC")
        self.to_top()
        self.assertEqual(self.sent("read_pane"), [])
        self.answer("wB:pC", unavailable="no-session")
        self.assertEqual([m["lines"] for m in self.sent("read_pane")], [600])
        self.assertFalse(self.inline_visible())

    def test_the_next_arrival_does_not_ask_again(self):
        self.open("wB:pC")
        self.to_top()
        self.answer("wB:pC", unavailable="no-session")
        self.scroll_to(100)
        self.to_top()
        self.assertEqual(len(self.sent("get_history")), 1)
        self.assertEqual([m["lines"] for m in self.sent("read_pane")], [600, 1000])

    def test_an_agent_with_nothing_left_says_why(self):
        self.open("wB:pD")
        self.to_top()
        self.answer("wB:pD", unavailable="no-session")
        self.assertEqual(self.sent("read_pane"), [])
        self.assertTrue(self.inline_visible())
        self.assertIn("Only the terminal output is available", self.inline_text())

    def test_the_verdict_belongs_to_one_pane(self):
        self.open("wB:pD")
        self.to_top()
        self.answer("wB:pD", unavailable="no-session")
        self.open("wB:pA")
        self.assertFalse(self.inline_visible())
        self.to_top()
        self.assertEqual(len(self.sent("get_history")), 1)


class LeavingTests(_Base):
    def test_returning_to_the_live_screen_puts_the_history_away(self):
        self.open("wB:pA")
        self.to_top()
        self.answer("wB:pA", messages=turns(0, 30), total=30)
        self.scroll_to(self.top() - 200)
        self.to_bottom()
        self.assertFalse(self.inline_visible())
        self.assertEqual(self.inline_text(), "")
        distance = self.page.eval_on_selector(
            "#termScroll", "e => e.scrollHeight - e.scrollTop - e.clientHeight")
        self.assertLessEqual(distance, 1)

    def test_the_next_visit_fetches_the_newest_page_again(self):
        """A kept page would leave a silent gap between its last turn and the live screen."""
        self.open("wB:pA")
        self.to_top()
        self.answer("wB:pA", messages=turns(0, 30), total=30)
        self.scroll_to(self.top() - 200)
        self.to_bottom()
        self.to_top()
        requests = self.sent("get_history")
        self.assertEqual(len(requests), 2)
        self.assertNotIn("before", requests[1])

    def test_a_page_landing_at_the_top_does_not_count_as_returning(self):
        """A mirror barely taller than its box is within 50px of the bottom at its own top."""
        for rows in range(10, 120):
            self.open("wB:pA", "\n".join(f"output line {i}" for i in range(rows)))
            overflow = self.page.eval_on_selector("#termScroll", "e => e.scrollHeight - e.clientHeight")
            if overflow > 0:
                break
        self.assertLess(overflow, 50)
        self.to_top()
        self.answer("wB:pA", messages=turns(0, 30), total=30)
        self.scroll_to(self.top())
        self.assertTrue(self.inline_visible())

    def test_a_selection_in_the_history_is_not_taken_away(self):
        self.open("wB:pA")
        self.to_top()
        self.answer("wB:pA", messages=turns(0, 30), total=30)
        self.scroll_to(self.top() - 200)
        self.page.evaluate("""() => {
          const node = document.querySelector('#termInlineHistory .msg-text');
          getSelection().selectAllChildren(node);
        }""")
        self.to_bottom()
        self.assertTrue(self.inline_visible())

    def test_a_real_switch_empties_it(self):
        self.open("wB:pA")
        self.to_top()
        self.answer("wB:pA", messages=turns(0, 30), total=30)
        self.open("wB:pB")
        self.assertFalse(self.inline_visible())
        self.assertEqual(self.inline_text(), "")

    def test_a_switch_mid_request_does_not_block_the_next_pane(self):
        """The answer for the pane you left is dropped, so nothing else clears the loading flag."""
        self.open("wB:pA")
        self.to_top()
        self.open("wB:pB")
        self.to_top()
        self.assertEqual([m["pane_id"] for m in self.sent("get_history")], ["wB:pB"])

    def test_a_blocked_re_entry_keeps_it(self):
        self.open("wB:pA")
        self.to_top()
        self.answer("wB:pA", messages=turns(0, 30), total=30)
        self.page.evaluate("openTerminal('wB:pA')")
        self.assertTrue(self.inline_visible())
        self.assertIn("turn number 29", self.inline_text())

    def test_a_lost_answer_does_not_leave_it_loading_after_a_reconnect(self):
        self.open("wB:pA")
        self.to_top()
        self.page.evaluate("historyConnectionReset()")
        self.assertFalse(self.inline_visible())
        self.scroll_to(100)
        self.to_top()
        self.assertEqual(len(self.sent("get_history")), 2)


class TheTickTests(_Base):
    def visit(self):
        self.open("wB:pA")
        self.to_top()
        self.answer("wB:pA", messages=turns(0, 60), total=60)
        self.scroll_to(100)

    def test_a_tick_does_not_move_a_reader_in_the_history(self):
        self.visit()
        before = self.line_y("turn number 10")
        grown = BODY + "\n" + "\n".join(f"new {i}" for i in range(30))
        self.page.evaluate("""body => handleMessage(
          {type: 'pane_content', pane_id: 'wB:pA', content: body})""", grown)
        self.assertAlmostEqual(self.line_y("turn number 10"), before, delta=1)

    def test_a_tick_still_keeps_up_at_the_bottom(self):
        self.open("wB:pA")
        self.page.evaluate("""body => handleMessage(
          {type: 'pane_content', pane_id: 'wB:pA', content: body + '\\nlast one'})""", BODY)
        distance = self.page.eval_on_selector(
            "#termScroll", "e => e.scrollHeight - e.scrollTop - e.clientHeight")
        self.assertLessEqual(distance, 1)

    def test_a_tick_while_the_page_is_loading_still_renders_the_output(self):
        self.open("wB:pA")
        self.to_top()
        self.page.evaluate("""() => handleMessage(
          {type: 'pane_content', pane_id: 'wB:pA', content: 'replaced'})""")
        self.assertEqual(
            self.page.eval_on_selector("#termContent", "e => e.textContent"), "replaced")


class MirrorUntouchedTests(_Base):
    def test_the_mirror_text_holds_only_the_terminal(self):
        """doSearch counts offsets in el.textContent, so a turn inside it would corrupt every hit."""
        self.open("wB:pA")
        self.to_top()
        self.answer("wB:pA", messages=turns(0, 5), total=5)
        self.assertEqual(self.page.eval_on_selector("#termContent", "e => e.textContent"), BODY)

    def test_find_in_output_does_not_search_the_history(self):
        self.open("wB:pA")
        self.to_top()
        self.answer("wB:pA", messages=turns(0, 5), total=5)
        self.page.evaluate("""() => {
          toggleSearch();
          document.getElementById('searchInput').value = 'turn number';
          doSearch();
        }""")
        self.assertEqual(self.page.evaluate("searchMatches.length"), 0)
        self.page.evaluate("hideSearch()")

    def test_a_long_line_scrolls_alone_and_the_turns_stay_on_screen(self):
        self.open("wB:pA", BODY + "\n" + "x" * 2000)
        self.to_top()
        self.answer("wB:pA", messages=turns(0, 5), total=5)
        result = self.page.evaluate("""() => {
          const term = document.getElementById('termContent');
          term.scrollLeft = 500;
          const scroller = document.getElementById('termScroll');
          return {term: term.scrollLeft, sideways: scroller.scrollWidth - scroller.clientWidth,
                  turnRight: document.querySelector('#termInlineHistory .msg-text')
                    .getBoundingClientRect().right, width: innerWidth};
        }""")
        self.assertEqual(result["term"], 500)
        self.assertEqual(result["sideways"], 0)
        self.assertLessEqual(result["turnRight"], result["width"])


class RealGestureTests(_Base):
    """Real input, because the mirror is a scroller inside a scroller. With `contain` left on its
    vertical axis, a pane holding one long line swallowed every vertical drag on a phone."""

    def test_a_wheel_over_a_mirror_with_a_long_line_reaches_the_conversation(self):
        self.open("wB:pA", BODY + "\n" + "x" * 2000)
        self.page.mouse.move(195, 400)
        for _ in range(40):
            self.page.mouse.wheel(0, -600)
            self.page.wait_for_timeout(40)
            if self.sent("get_history"):
                break
        self.assertEqual(len(self.sent("get_history")), 1)


class SharedWithThePanelTests(_Base):
    def test_opening_the_panel_over_it_reuses_what_is_loaded(self):
        self.open("wB:pA")
        self.to_top()
        self.answer("wB:pA", messages=turns(0, 30), total=30)
        self.page.evaluate("toggleHistory()")
        self.assertEqual(len(self.sent("get_history")), 1)
        self.assertIn("turn number 29",
                      self.page.eval_on_selector("#historyContent", "e => e.textContent"))
        self.assertIn("turn number 29", self.inline_text())
        self.page.evaluate("toggleHistory()")

    def test_opening_the_panel_alone_still_fetches(self):
        self.open("wB:pA")
        self.page.evaluate("toggleHistory()")
        self.assertEqual(len(self.sent("get_history")), 1)
        self.assertFalse(self.inline_visible())
        self.answer("wB:pA", messages=turns(0, 3), total=3)
        self.assertFalse(self.inline_visible())
        self.page.evaluate("toggleHistory()")

    def test_a_turn_renders_as_the_panel_renders_it(self):
        self.open("wB:pA")
        self.to_top()
        self.answer("wB:pA", messages=turns(0, 4), total=4)
        look = self.page.evaluate("""() => {
          const bubble = document.querySelector('#termInlineHistory .msg.user');
          return {maxWidth: getComputedStyle(bubble).maxWidth,
                  whiteSpace: getComputedStyle(bubble).whiteSpace};
        }""")
        self.assertEqual(look["maxWidth"], "88%")
        self.assertEqual(look["whiteSpace"], "normal")


class SameGroundAsTheTerminalTests(_Base):
    """The two read as one page, so they share one ground under every theme."""

    def setUp(self):
        super().setUp()
        self.open("wB:pA")
        self.to_top()
        self.answer("wB:pA", messages=turns(0, 4), total=4)

    def tearDown(self):
        self.page.evaluate("""() => {
          const root = document.documentElement;
          delete root.dataset.theme;
          root.removeAttribute('style');
        }""")
        super().tearDown()

    def look(self):
        return self.page.evaluate("""() => {
          const css = s => getComputedStyle(document.querySelector(s));
          return {inline: css('#termInlineHistory').backgroundColor,
                  term: css('#termContent').backgroundColor,
                  text: css('#termInlineHistory .msg.assistant .msg-text').color,
                  muted: css('#termInlineHistory .msg-role').color,
                  bubble: css('#termInlineHistory .msg.user .msg-text').backgroundColor,
                  scheme: css('#termInlineHistory').colorScheme};
        }""")

    def assert_dark(self):
        look = self.look()
        self.assertEqual(look["inline"], look["term"])
        self.assertEqual(look["text"], "rgb(250, 250, 250)")
        self.assertEqual(look["scheme"], "dark")
        # Either a plain dark value or a mix: whatever the form, not a light fill.
        self.assertNotIn("255, 255, 255", look["bubble"])
        self.assertNotRegex(look["bubble"], r"0\.9\d*\s*\d*\.?\d*\s*\d*\.?\d*\s*\)$")

    def test_under_the_light_theme(self):
        self.page.evaluate("document.documentElement.dataset.theme = 'light'")
        self.assert_dark()

    def test_under_the_dark_theme(self):
        self.page.evaluate("document.documentElement.dataset.theme = 'dark'")
        self.assert_dark()

    def test_under_a_light_custom_palette(self):
        """Its dark text would otherwise land on the terminal's dark ground, unreadable."""
        self.page.evaluate("""() => {
          const root = document.documentElement;
          root.dataset.theme = 'light';
          for (const [k, v] of Object.entries({bg: '#fff8e7', surface: '#ffffff', border: '#ccbbaa',
              text: '#201000', muted: '#554433', green: '#005500', red: '#880000', blue: '#000088',
              orange: '#663300', 'on-accent': '#ffffff'})) root.style.setProperty('--' + k, v);
        }""")
        self.assert_dark()

    def test_the_page_around_it_keeps_its_theme(self):
        self.page.evaluate("document.documentElement.dataset.theme = 'light'")
        self.assertEqual(
            self.page.eval_on_selector(".term-header", "e => getComputedStyle(e).colorScheme"),
            "light")


class ShortOutputTests(_Base):
    """A desktop window can fit the whole screen, and then no scroll event can ever fire."""
    viewport = DESKTOP
    SHORT = "one\ntwo\nthree"

    def wheel_up(self):
        self.page.dispatch_event("#termScroll", "wheel", {"deltaY": -120})

    def test_a_wheel_up_brings_the_conversation_in(self):
        self.open("wB:pA", self.SHORT)
        self.wheel_up()
        self.assertEqual(len(self.sent("get_history")), 1)

    def test_a_wheel_down_does_not(self):
        self.open("wB:pA", self.SHORT)
        self.page.dispatch_event("#termScroll", "wheel", {"deltaY": 120})
        self.assertEqual(self.sent("get_history"), [])

    def test_a_terminal_ignores_the_wheel(self):
        self.open("wB:pS", self.SHORT)
        self.wheel_up()
        self.assertEqual(self.sent("get_history"), [])
        self.assertEqual(self.sent("read_pane"), [])

    def test_a_short_mirror_still_fills_the_box(self):
        self.open("wB:pA", self.SHORT)
        boxes = self.page.evaluate("""() => {
          const s = document.getElementById('termScroll').getBoundingClientRect();
          const t = document.getElementById('termContent').getBoundingClientRect();
          return [s.height, t.height];
        }""")
        self.assertAlmostEqual(boxes[0], boxes[1], delta=1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
