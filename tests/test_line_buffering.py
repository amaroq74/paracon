# =============================================================================
# Tests for _gather_lines / partial-line display and the partial-escape guard
# regex (_ANSI_PARTIAL_ESC_RE) in paracon/paracon.py.
#
# Display rules:
#   - CR/LF-terminated text     -> add_line() -- a permanent new row.
#   - Unterminated normal text  -> _show_partial() -- shown immediately,
#                                  updated in-place when continuation arrives.
#   - Partial escape at end     -> held; _show_partial() called only after
#                                  the timeout safety-net fires (data loss).
# =============================================================================

import importlib.util
import pathlib
import sys
import types
from unittest.mock import MagicMock

import pytest

# -----------------------------------------------------------------------------
# Minimal stubs so paracon.py can be imported without a running urwid / AGWPE
# -----------------------------------------------------------------------------

def _urwid_stub():
    mod = types.ModuleType('urwid')
    mod.ListBox = object
    mod.WidgetWrap = object
    mod.__getattr__ = lambda name: MagicMock()
    return mod


_STUBS = {
    'urwid':       _urwid_stub(),
    'ax25':        MagicMock(),
    'ax25.netrom': MagicMock(),
    'config':      MagicMock(),
    'pserver':     MagicMock(),
    'urwidx':      MagicMock(),
}

for _name, _stub in _STUBS.items():
    sys.modules.pop(_name, None)
    sys.modules[_name] = _stub

# Load paracon/paracon.py by file path to avoid package-naming conflicts.
_PARACON_FILE = pathlib.Path(__file__).parent.parent / 'paracon' / 'paracon.py'
_spec = importlib.util.spec_from_file_location('_paracon_impl', _PARACON_FILE)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

_ANSI_PARTIAL_ESC_RE  = _mod._ANSI_PARTIAL_ESC_RE
_LINE_REMAINS_TIMEOUT = _mod._LINE_REMAINS_TIMEOUT


# -----------------------------------------------------------------------------
# Test harness
#
# Borrows _gather_lines / _decode_line from ConnectionPanel and supplies
# lightweight _show_partial / _finalize_partial / add_line stubs that record
# the visible screen state in self.rows (a list of strings, mutated in-place
# for partial updates, exactly as the real UI does with urwid widgets).
# -----------------------------------------------------------------------------

class _Panel:
    """Duck-typed stand-in for ConnectionPanel (line-buffering methods only)."""

    _gather_lines = _mod.ConnectionPanel._gather_lines
    _decode_line  = _mod.ConnectionPanel._decode_line

    def __init__(self, decoders=None):
        self._line_remains = b''
        self._line_remains_time = 0.0
        self._partial_widget = None   # index into self.rows, or None
        self._decoders = decoders or ['utf-8']
        self.rows = []   # current visible screen rows (strings)

    # -- complete line (permanent) -------------------------------------------
    def add_line(self, line):
        if self._partial_widget is not None:
            self._line_remains = b''
        self._partial_widget = None
        self.rows.append(line if isinstance(line, str) else str(line))

    # -- partial line (may be updated in-place) --------------------------------
    def _show_partial(self, line):
        text = line if isinstance(line, str) else str(line)
        if self._partial_widget is None:
            self.rows.append(text)
            self._partial_widget = len(self.rows) - 1   # index of partial row
        else:
            self.rows[self._partial_widget] = text      # in-place update

    def _finalize_partial(self, line):
        text = line if isinstance(line, str) else str(line)
        self.rows[self._partial_widget] = text          # final in-place update
        self._partial_widget = None

    # -- simulate end-of-queue-drain flush ------------------------------------
    def flush_remains(self, elapsed=_LINE_REMAINS_TIMEOUT + 1):
        """Mirrors the end-of-queue-drain flush in _update_from_queue.

        elapsed: simulated seconds since _line_remains was last set.
        Defaults to just past the timeout so safety-net tests work without
        setting it explicitly.
        """
        if self._line_remains:
            if _ANSI_PARTIAL_ESC_RE.search(self._line_remains):
                if elapsed > _LINE_REMAINS_TIMEOUT:
                    self._show_partial(self._decode_line(self._line_remains))
            else:
                self._show_partial(self._decode_line(self._line_remains))


# =============================================================================
# _ANSI_PARTIAL_ESC_RE
# =============================================================================

class TestPartialEscapeRegex:
    """Regex must flag only incomplete CSI SGR sequences at the end of bytes."""

    @pytest.mark.parametrize("data", [
        b'\x1b',            # bare ESC
        b'\x1b[',           # ESC + CSI introducer, no params
        b'\x1b[3',          # one digit, no closing 'm'
        b'\x1b[31',         # typical partial colour code
        b'\x1b[31;1',       # multi-param, no closing 'm'
        b'hello\x1b[32',    # text followed by partial escape
    ])
    def test_matches_partial_escape(self, data):
        assert _ANSI_PARTIAL_ESC_RE.search(data), repr(data)

    @pytest.mark.parametrize("data", [
        b'Post message? [Y/N]:',   # no escape at all
        b'hello world\r',          # normal CR-terminated line
        b'\x1b[31mhello',          # complete SGR (has closing 'm')
        b'\x1b[0m',                # reset sequence
        b'line\x1b[1;32mtext',     # complete multi-param SGR
        b'',                       # empty
    ])
    def test_no_match_for_complete_or_plain(self, data):
        assert not _ANSI_PARTIAL_ESC_RE.search(data), repr(data)


# =============================================================================
# _gather_lines  (CR/LF splitting and in-progress buffering)
# =============================================================================

class TestGatherLines:

    def test_cr_terminated_line(self):
        p = _Panel()
        p._gather_lines(b'hello\r')
        assert p.rows == ['hello']
        assert p._line_remains == b''

    def test_lf_terminated_line(self):
        p = _Panel()
        p._gather_lines(b'hello\n')
        assert p.rows == ['hello']

    def test_crlf_terminated_line(self):
        p = _Panel()
        p._gather_lines(b'hello\r\n')
        assert p.rows == ['hello']

    def test_unterminated_fragment_buffered(self):
        """Fragment without CR stays in _line_remains; nothing shown yet."""
        p = _Panel()
        p._gather_lines(b'Bulletin')
        assert p.rows == []
        assert p._line_remains == b'Bulletin'

    def test_multiple_cr_terminated_lines(self):
        p = _Panel()
        p._gather_lines(b'line1\rline2\rline3\r')
        assert p.rows == ['line1', 'line2', 'line3']

    def test_trailing_fragment_held_after_complete_lines(self):
        p = _Panel()
        p._gather_lines(b'line1\rpartial')
        assert p.rows == ['line1']
        assert p._line_remains == b'partial'

    def test_line_remains_time_updated(self):
        p = _Panel()
        p._gather_lines(b'prompt: ')
        assert p._line_remains_time > 0.0

    def test_fragment_combined_within_same_poll_cycle(self):
        """Two frames queued together in the same poll cycle are joined before
        display -- no flush between them."""
        p = _Panel()
        p._gather_lines(b'Bulletin')
        p._gather_lines(b's\r')
        assert p.rows == ['Bulletins']


# =============================================================================
# _show_partial / _finalize_partial  (in-place widget update model)
# =============================================================================

class TestPartialDisplay:

    def test_bbs_prompt_shown_immediately_after_flush(self):
        """'Post message? [Y/N]:' must appear on screen as soon as the
        queue is drained -- no timeout, no delay."""
        p = _Panel()
        p._gather_lines(b'Post message? [Y/N]:')
        assert p.rows == []            # not shown mid-gather
        p.flush_remains(elapsed=0)     # called instantly -- still shown
        assert p.rows == ['Post message? [Y/N]:']

    def test_partial_text_updated_in_place_on_continuation(self):
        """Frame 1 shows 'Bulletin' as a partial row; frame 2's 's\\r'
        updates that same row to 'Bulletins' rather than adding a new one."""
        p = _Panel()
        p._gather_lines(b'Bulletin')
        p.flush_remains(elapsed=0)
        assert p.rows == ['Bulletin']          # partial row shown
        assert p._partial_widget is not None   # still marked as partial

        p._gather_lines(b's\r')
        assert p.rows == ['Bulletins']         # updated in-place
        assert p._partial_widget is None       # now finalized

    def test_partial_then_more_unterminated(self):
        """Partial widget keeps being updated in-place as more unterminated
        content arrives across multiple poll cycles."""
        p = _Panel()
        p._gather_lines(b'Sel')
        p.flush_remains(elapsed=0)
        assert p.rows == ['Sel']

        p._gather_lines(b'ect')
        p.flush_remains(elapsed=0)
        # _gather_lines combined 'Sel'+'ect' -> _line_remains = b'Select',
        # flush updates partial row in-place
        assert p.rows == ['Select']
        assert len(p.rows) == 1        # still just one row

    def test_complete_line_after_partial(self):
        """After a partial row, a CR-terminated continuation finalizes it
        and further complete lines go to new rows."""
        p = _Panel()
        p._gather_lines(b'Enter choice: ')
        p.flush_remains(elapsed=0)
        assert p.rows == ['Enter choice: ']

        p._gather_lines(b'A\r[A] Areas\r')
        assert p.rows == ['Enter choice: A', '[A] Areas']

    def test_add_line_clears_partial(self):
        """A system message (add_line) after a partial row leaves the
        partial text as-is and starts fresh."""
        p = _Panel()
        p._gather_lines(b'partial')
        p.flush_remains(elapsed=0)
        assert p.rows == ['partial']

        p.add_line('System message')
        assert p.rows == ['partial', 'System message']
        assert p._partial_widget is None

    def test_add_line_clears_line_remains_to_prevent_duplicate(self):
        """Regression: if a BBS prompt is showing as a partial row and the
        user sends a command (triggering add_line for the outbound echo),
        the prompt must NOT reappear on the next flush.

        Flow:
          1. BBS sends 'Enter choice: ' (no CR) -> shown as partial row
          2. User types 'Q' -> add_line(outbound 'Q') -> new permanent row
          3. Next poll flush -> must NOT redisplay 'Enter choice: '
        """
        p = _Panel()
        p._gather_lines(b'Enter choice: ')
        p.flush_remains(elapsed=0)
        assert p.rows == ['Enter choice: ']

        # Simulates _send() adding the outbound echo
        p.add_line('Q')
        assert p.rows == ['Enter choice: ', 'Q']
        assert p._line_remains == b''          # cleared by add_line

        # Next poll cycle -- must not re-add 'Enter choice: '
        p.flush_remains(elapsed=0)
        assert p.rows == ['Enter choice: ', 'Q']  # no duplicate

    def test_flush_noop_when_nothing_buffered(self):
        p = _Panel()
        p.flush_remains()
        assert p.rows == []


# =============================================================================
# Partial escape sequences  (held until complete; timeout as safety net)
# =============================================================================

class TestPartialEscape:

    def test_partial_escape_not_shown_before_timeout(self):
        """Incomplete escape sequence must not be shown prematurely."""
        p = _Panel()
        p._gather_lines(b'\x1b[31')
        p.flush_remains(elapsed=0.5)
        assert p.rows == []
        assert p._line_remains == b'\x1b[31'

    def test_partial_escape_shown_raw_after_timeout(self):
        """Safety net: flush as raw text when completing bytes never arrive."""
        p = _Panel()
        p._gather_lines(b'\x1b[31')
        p.flush_remains()              # default elapsed > timeout
        assert p.rows == ['\x1b[31']

    def test_partial_escape_completed_by_next_frame(self):
        """Completing bytes in the next frame join with the held fragment."""
        p = _Panel()
        p._gather_lines(b'\x1b[31')
        p.flush_remains(elapsed=0)     # within timeout -- not shown
        assert p.rows == []

        p._gather_lines(b'mERROR\r')
        assert p.rows == ['\x1b[31mERROR']
