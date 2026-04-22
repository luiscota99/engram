"""
Engram TUI Browser — interactive memory explorer using curses.

Launch with: engram browse

Controls:
  Arrow keys / j/k     Navigate list
  /                    Search (type query, Enter to confirm, Esc to cancel)
  Tab                  Cycle memory type filter (All / mistake / pattern / skill / session)
  Enter                View full detail of selected item
  q / Esc              Quit (or close detail view)
"""
from __future__ import annotations

import curses
import textwrap

from .search import get_recent, search

TYPES = ["all", "mistake", "pattern", "skill", "session"]
TYPE_COLORS = {
    "mistake": 1,
    "pattern": 2,
    "skill": 3,
    "session": 4,
    "conversation": 5,
    "all": 0,
}

HELP_LINE = "  [↑↓/jk] Navigate  [/] Search  [Tab] Filter  [Enter] Detail  [q] Quit"


def _init_colors():
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_RED, -1)
    curses.init_pair(2, curses.COLOR_YELLOW, -1)
    curses.init_pair(3, curses.COLOR_GREEN, -1)
    curses.init_pair(4, curses.COLOR_CYAN, -1)
    curses.init_pair(5, curses.COLOR_BLUE, -1)
    curses.init_pair(6, curses.COLOR_WHITE, curses.COLOR_BLUE)   # selected row
    curses.init_pair(7, curses.COLOR_BLACK, curses.COLOR_WHITE)  # status bar


def _type_badge(item_type: str) -> str:
    return f"[{item_type.upper()[:3]}]"


def _draw_status(win, h, w, msg: str, style=7):
    win.attron(curses.color_pair(style))
    win.addnstr(h - 1, 0, msg.ljust(w), w)
    win.attroff(curses.color_pair(style))


def _draw_list(win, h, w, results, selected, offset):
    list_h = h - 3
    for i in range(list_h):
        idx = offset + i
        y = i + 1
        win.move(y, 0)
        win.clrtoeol()
        if idx >= len(results):
            continue
        r = results[idx]
        itype = r.get("item_type", "")
        color = TYPE_COLORS.get(itype, 0)
        badge = _type_badge(itype)
        title = r.get("title") or r.get("snippet", "")[:60]
        tags = r.get("tags", "")
        line = f" {badge} {title}"
        if tags:
            line += f"  [{tags}]"
        if idx == selected:
            win.attron(curses.color_pair(6) | curses.A_BOLD)
            win.addnstr(y, 0, line.ljust(w), w)
            win.attroff(curses.color_pair(6) | curses.A_BOLD)
        else:
            win.attron(curses.color_pair(color) | curses.A_BOLD)
            win.addstr(y, 1, badge)
            win.attroff(curses.color_pair(color) | curses.A_BOLD)
            win.addnstr(y, 1 + len(badge), line[1 + len(badge):].ljust(w - 1 - len(badge)), w - 1 - len(badge))


def _draw_header(win, w, query: str, type_filter: str, total: int):
    filter_label = f"[{type_filter.upper()}]" if type_filter != "all" else "[ALL]"
    q_label = f'  search: "{query}"' if query else "  (recent)"
    header = f" Engram Browse  {filter_label}{q_label}  {total} result(s)"
    win.attron(curses.color_pair(7) | curses.A_BOLD)
    win.addnstr(0, 0, header.ljust(w), w)
    win.attroff(curses.color_pair(7) | curses.A_BOLD)


def _read_search_query(win, h, w, current: str) -> str | None:
    """Inline search prompt at the bottom of the screen. Returns query or None on Esc."""
    prompt = " / "
    buf = list(current)
    while True:
        query_line = prompt + "".join(buf) + "_"
        win.attron(curses.color_pair(7))
        win.addnstr(h - 1, 0, query_line.ljust(w), w)
        win.attroff(curses.color_pair(7))
        win.refresh()
        ch = win.getch()
        if ch in (curses.KEY_ENTER, 10, 13):
            return "".join(buf).strip()
        elif ch == 27:  # Esc
            return None
        elif ch in (curses.KEY_BACKSPACE, 127, 8):
            if buf:
                buf.pop()
        elif 32 <= ch <= 126:
            buf.append(chr(ch))


def _show_detail(stdscr, item: dict):
    """Full-screen detail view for a single memory item."""
    h, w = stdscr.getmaxyx()
    detail_win = curses.newwin(h, w, 0, 0)
    detail_win.keypad(True)

    lines: list[str] = []
    itype = item.get("item_type", "")
    item_id = item.get("item_id", "?")
    title = item.get("title", "")
    snippet = item.get("snippet", "")
    tags = item.get("tags", "")

    lines.append(f"[{itype.upper()}]  #{item_id}  {title}")
    lines.append("─" * min(w - 2, 60))
    if tags:
        lines.append(f"Tags: {tags}")
        lines.append("")
    for para in snippet.split(" | "):
        para = para.strip()
        if para:
            wrapped = textwrap.wrap(para, width=w - 4) or [para]
            lines.extend(wrapped)
            lines.append("")

    scroll = 0
    max_scroll = max(0, len(lines) - (h - 3))

    while True:
        detail_win.erase()
        detail_win.attron(curses.color_pair(7) | curses.A_BOLD)
        hdr = f" {_type_badge(itype)} {title}"
        detail_win.addnstr(0, 0, hdr.ljust(w), w)
        detail_win.attroff(curses.color_pair(7) | curses.A_BOLD)

        visible = h - 3
        for i in range(visible):
            li = scroll + i
            if li < len(lines):
                detail_win.addnstr(i + 1, 2, lines[li][:w - 2], w - 2)

        _draw_status(detail_win, h, w, "  [↑↓/jk] Scroll  [q/Esc] Back")
        detail_win.refresh()

        ch = detail_win.getch()
        if ch in (ord("q"), 27):
            break
        elif ch in (curses.KEY_UP, ord("k")):
            scroll = max(0, scroll - 1)
        elif ch in (curses.KEY_DOWN, ord("j")):
            scroll = min(max_scroll, scroll + 1)
        elif ch == curses.KEY_PPAGE:
            scroll = max(0, scroll - (visible - 1))
        elif ch == curses.KEY_NPAGE:
            scroll = min(max_scroll, scroll + (visible - 1))


def _run_browser(stdscr):
    _init_colors()
    curses.curs_set(0)
    stdscr.keypad(True)

    query = ""
    type_idx = 0
    selected = 0
    offset = 0

    def load_results():
        t = TYPES[type_idx]
        if query:
            return search(query, item_type=None if t == "all" else t, limit=200)
        else:
            return get_recent(limit=50, item_type=None if t == "all" else t)

    results = load_results()

    while True:
        h, w = stdscr.getmaxyx()
        list_h = h - 3

        # Clamp selection
        if results:
            selected = max(0, min(selected, len(results) - 1))
            if selected < offset:
                offset = selected
            elif selected >= offset + list_h:
                offset = selected - list_h + 1
        else:
            selected = offset = 0

        stdscr.erase()
        _draw_header(stdscr, w, query, TYPES[type_idx], len(results))
        _draw_list(stdscr, h, w, results, selected, offset)

        if not results:
            msg = "  No results." if query else "  No memories yet."
            stdscr.addstr(2, 2, msg)

        _draw_status(stdscr, h, w, HELP_LINE)
        stdscr.refresh()

        ch = stdscr.getch()

        if ch in (ord("q"), 27):
            break
        elif ch in (curses.KEY_UP, ord("k")):
            selected = max(0, selected - 1)
        elif ch in (curses.KEY_DOWN, ord("j")):
            selected = min(max(len(results) - 1, 0), selected + 1)
        elif ch == curses.KEY_PPAGE:
            selected = max(0, selected - list_h)
        elif ch == curses.KEY_NPAGE:
            selected = min(max(len(results) - 1, 0), selected + list_h)
        elif ch == ord("\t"):
            type_idx = (type_idx + 1) % len(TYPES)
            selected = offset = 0
            results = load_results()
        elif ch == ord("/"):
            new_q = _read_search_query(stdscr, h, w, query)
            if new_q is not None:
                query = new_q
                selected = offset = 0
                results = load_results()
        elif ch in (curses.KEY_ENTER, 10, 13):
            if results and selected < len(results):
                _show_detail(stdscr, results[selected])
        elif ch == curses.KEY_RESIZE:
            pass  # redraws automatically on next loop


def run_browser():
    """Entry point for `engram browse`."""
    try:
        curses.wrapper(_run_browser)
    except KeyboardInterrupt:
        pass
