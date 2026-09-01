"""The on-screen overlay window. Tk lives here and nowhere else.

Three constraints shaped this file:

* **Tk is not thread-safe, and pystray already owns the main thread.** So
  the window gets its own thread with its own `Tk()` root, and the capture
  loop talks to it through a queue that Tk drains with `after()`. Nothing
  outside this module ever touches a widget.
* **It must be impossible for the overlay to break the app.** A machine
  without Tk, a remote session with no display, a driver that refuses
  transparency -- all of these end with `build_overlay` returning a
  `NullOverlay` and the keyboard panel carrying on. Every entry point here
  is wrapped accordingly.
* **It must not steal focus.** Elden Ring in borderless windowed loses
  input if another window takes focus, so the overlay never calls
  `focus_set`, never uses a normal titlebar, and is created withdrawn.

Anti-cheat note: this draws a window. It reads nothing from the game and
opens no handle to it, exactly like the rest of ERDLE.
"""

from __future__ import annotations

import os
import queue
import threading
from dataclasses import dataclass
from typing import Callable

from .bossdb import IMMUNE, NORMAL, RESISTANT, WEAK
from .overlay import OverlayContent

# Elden Ring's palette, roughly: parchment on near-black, gold for headings.
BACKGROUND = "#12100e"
BORDER = "#c49e4a"
TITLE_FG = "#e8dcc0"
HEADLINE_FG = "#9a9086"
SECTION_FG = "#c49e4a"
LABEL_FG = "#cdc4b4"
UNKNOWN_FG = "#5d574f"
TRACK = "#2a2622"

EFFECT_FG = {
    WEAK: "#8fd07a",
    NORMAL: "#b8b0a0",
    RESISTANT: "#d9a441",
    IMMUNE: "#c95f5f",
}

# Shown instead of the effectiveness word when the database has no value.
UNKNOWN_TEXT = "no data"

PANEL_WIDTH = 300
PADDING = 16

# Vertical rhythm. The first version packed rows 17px apart with 9pt text,
# which left about four pixels of air and read as a solid block -- fine on
# a spreadsheet, useless at a glance mid-fight. These are the numbers that
# make each row legible on its own.
ROW_HEIGHT = 19
TITLE_HEIGHT = 19
HEADLINE_HEIGHT = 14
AFTER_TITLE = 4
AFTER_HEADLINE = 10
SECTION_HEADER = 16
BETWEEN_SECTIONS = 12
BEFORE_POISE = 8

# --- font sizes -------------------------------------------------------------
# Points at 96 DPI, converted to pixels by `OverlayStyle.font`. Collected
# here rather than left inline so the panel can be resized from one place;
# they were scattered across `_draw` and `_section` as bare literals.
#
# Shrinking these alone makes the panel *look* smaller but not *be*
# smaller -- the row heights above are what set its footprint, so the two
# blocks are meant to move together. As a rule of thumb keep ROW_HEIGHT
# about twice ROW_FONT, or the value column starts touching the row below.
TITLE_FONT = 11        # the boss name
HEADLINE_FONT = 8      # the one-line summary, when it is shown at all
SECTION_FONT = 7       # "DAMAGE" / "STATUS"
ROW_FONT = 8           # every label and value
POISE_FONT = 8

#: The summary line under the name ("immune to frost; weak to slash ...")
#: repeats what the rows below already say, so it is off by default and
#: kept only for the case where there are no rows at all -- seven bosses
#: have completely flat stats, and a panel showing just a name reads as a
#: rendering fault. Set True to get it back on every boss.
SHOW_HEADLINE = False

BAR_WIDTH = 46
BAR_HEIGHT = 5

#: Font sizes below are written as points at 96 DPI, because that is how
#: they were designed, and converted to pixels so they stay locked to the
#: layout constants. 72pt to the inch, 96px to the inch.
POINTS_TO_PIXELS = 96 / 72

#: Two ways to read the same boss. "compact" shows only the rows that
#: change a decision -- what to bring, what to avoid, which statuses are
#: unusual. "full" shows all eight damage types and all six statuses.
COMPACT, FULL = "compact", "full"


@dataclass
class OverlayStyle:
    scale: float = 1.0
    opacity: float = 0.88
    detail: str = COMPACT

    def px(self, value: float) -> int:
        return max(1, int(round(value * self.scale)))

    def font(self, size: int, weight: str = "normal") -> tuple:
        """A font sized in *pixels*, not points.

        Tk reads a positive size as points and converts using the display
        DPI, so at 150% scaling a "12pt" heading renders 24px tall while
        every layout constant in this file stayed at 96-DPI pixels. Rows
        then overlapped, and worst on long names, because a title that
        needed two lines at 96 DPI needed three.

        A negative size means pixels, which puts the text in the same unit
        as the layout. The panel is then scaled as a whole by `scale`,
        which `build_overlay` seeds from the real DPI -- so it still looks
        right on a 4K display, but nothing can drift out of proportion.
        """
        pixels = max(7, int(round(size * POINTS_TO_PIXELS * self.scale)))
        return ("Segoe UI", -pixels, weight)


def enable_dpi_awareness() -> str:
    """Make this process report real pixels on Windows.

    A DPI-unaware process sees a virtualised desktop: at 150% scaling
    Windows tells it the screen is 2560 wide when it is really 3840, then
    multiplies every coordinate the process sets. `winfo_screenwidth`,
    `geometry(+x+y)` and `winfo_x` then disagree about what a pixel is, and
    a window aimed at the right-hand edge lands somewhere near the middle.

    Must be called before the first window exists. Returns a short string
    describing what happened, for the log -- failure is not worth raising
    over, since the overlay still works, just in the wrong place.
    """
    if os.name != "nt":
        return "not windows"
    import ctypes

    try:
        # PROCESS_PER_MONITOR_DPI_AWARE. Windows 8.1+.
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return "per-monitor"
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
        return "system"
    except Exception:
        return "unavailable"


class NullOverlay:
    """Stands in when the overlay cannot or should not run.

    Exists so callers never have to check. Every method is a no-op, so the
    difference between "overlay disabled" and "Tk missing" and "overlay
    working" is invisible to the capture loop.
    """

    available = False

    def __init__(self, reason: str = "disabled") -> None:
        #: Why there is no overlay. Without this the three failure paths
        #: -- no Tk, constructor raised, Tk thread never produced a root
        #: -- are indistinguishable from "switched off", which is exactly
        #: the report that is impossible to act on.
        self.unavailable_reason = reason

    def start(self) -> None:
        pass

    def show(self, content: OverlayContent) -> None:
        pass

    def hide(self) -> None:
        pass

    def set_detail(self, detail: str) -> None:
        pass

    def set_enabled(self, enabled: bool) -> None:
        pass

    def stop(self) -> None:
        pass


class OverlayWindow:
    """A borderless, always-on-top panel driven from another thread."""

    available = True

    def __init__(
        self,
        *,
        style: OverlayStyle | None = None,
        position: tuple[float | None, float | None] = (None, None),
        legacy_pixels: tuple[int | None, int | None] = (None, None),
        on_move: Callable[[float, float], None] | None = None,
        enabled: bool = True,
    ) -> None:
        self.style = style or OverlayStyle()
        self.enabled = enabled
        #: Fractions, not pixels. See `_x`.
        self._wanted = position
        #: Raw pixels from a pre-fraction config, converted on first use.
        self._legacy = legacy_pixels
        self._on_move = on_move
        self._commands: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._root = None
        self._canvas = None
        self._visible = False
        self._drag_origin: tuple[int, int] | None = None
        #: (wanted_x, wanted_y, actual_x, actual_y, outcome) from the last
        #: placement. Diagnostics only.
        self.last_placement: tuple | None = None
        self.dpi_mode = "unknown"
        self.screen: tuple[int, int] | None = None
        #: How much the display's DPI grew the panel. 1.0 at 96 DPI.
        self.display_scale = 1.0
        #: Whatever killed the Tk thread, kept so `build_overlay` can say
        #: so rather than reporting a bare timeout.
        self.start_error: str | None = None
        #: The last thing drawn, so a settings change can repaint it
        #: without waiting for the next poll.
        self._last_content: OverlayContent | None = None

    # --- public API, safe from any thread ---------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="erdle-overlay", daemon=True
        )
        self._thread.start()
        # Bounded wait: if Tk cannot start we carry on without an overlay
        # rather than hanging the capture loop behind it.
        self._ready.wait(timeout=5.0)

    def show(self, content: OverlayContent) -> None:
        self._commands.put(("show", content))

    def hide(self) -> None:
        self._commands.put(("hide", None))

    def set_detail(self, detail: str) -> None:
        """Switch between the compact and full views, live.

        Queued like everything else, because the tray menu runs on the
        Win32 message-loop thread and Tk widgets belong to theirs.
        """
        self._commands.put(("detail", detail))

    def set_enabled(self, enabled: bool) -> None:
        self._commands.put(("enabled", bool(enabled)))

    def stop(self) -> None:
        self._commands.put(("stop", None))
        thread = self._thread
        if thread is not None:
            thread.join(timeout=3.0)

    # --- the Tk thread ----------------------------------------------------

    def _run(self) -> None:
        try:
            import tkinter as tk
        except Exception as exc:
            self.start_error = f"tkinter did not import: {exc}"
            self._ready.set()
            return

        self.dpi_mode = enable_dpi_awareness()
        try:
            root = tk.Tk()
            root.withdraw()
            root.overrideredirect(True)
            root.attributes("-topmost", True)
            try:
                root.attributes("-alpha", self.style.opacity)
            except Exception:
                pass  # some X servers and RDP sessions refuse alpha
            root.configure(bg=BORDER)

            # Before the canvas exists, not after: the canvas is sized
            # from `style.px`, and scaling the style afterwards left the
            # drawing 50% wider than the surface it was drawn on. Names
            # ran off the right edge and the whole value column vanished.
            self._apply_display_scale(root)

            canvas = tk.Canvas(
                root,
                width=self.style.px(PANEL_WIDTH),
                height=self.style.px(120),
                bg=BACKGROUND,
                highlightthickness=0,
                bd=0,
            )
            # One pixel of border colour showing round the canvas is the
            # whole frame. Cheaper and crisper than drawing a rectangle.
            canvas.pack(padx=1, pady=1)

            canvas.bind("<Button-1>", self._grab)
            canvas.bind("<B1-Motion>", self._drag)
            canvas.bind("<ButtonRelease-1>", self._drop)
        except Exception as exc:
            self.start_error = f"{type(exc).__name__}: {exc}"
            self._ready.set()
            return

        self._root = root
        self._canvas = canvas
        try:
            self.screen = (root.winfo_screenwidth(), root.winfo_screenheight())
        except Exception:
            self.screen = None
        self._ready.set()

        root.after(40, self._pump)
        try:
            root.mainloop()
        except Exception as exc:
            self.start_error = f"mainloop stopped: {type(exc).__name__}: {exc}"

    def _apply_display_scale(self, root) -> None:
        """Grow the whole panel on a high-DPI display.

        Fonts are pinned to pixels so the layout cannot drift, which on its
        own would leave a postage-stamp panel on a 4K screen at 150%. So
        the DPI is folded into `scale` instead: everything grows together,
        in proportion, and the arithmetic stays in one unit.

        The user's own `overlay_scale` still multiplies on top, so someone
        who wants it bigger or smaller than the system default can say so.
        """
        try:
            dpi = float(root.winfo_fpixels("1i"))
        except Exception:
            return
        if dpi <= 0:
            return
        self.display_scale = max(1.0, min(3.0, dpi / 96.0))
        self.style.scale *= self.display_scale

    def _pump(self) -> None:
        """Drain the command queue on the Tk thread."""
        root = self._root
        if root is None:
            return
        try:
            while True:
                action, payload = self._commands.get_nowait()
                if self.dispatch(action, payload):
                    try:
                        root.quit()
                        root.destroy()
                    except Exception:
                        pass
                    # The canvas has to go too. Clearing only `_root` left
                    # a live widget holding the Tcl interpreter open, so
                    # it was finalised from the main thread at exit and
                    # printed "Tcl_AsyncDelete: async handler deleted by
                    # the wrong thread" after every shutdown.
                    self._root = None
                    self._canvas = None
                    self._visible = False
                    return
        except queue.Empty:
            pass
        except Exception:
            # A drawing failure must not kill the pump, or the overlay
            # freezes on a stale boss for the rest of the session.
            pass
        root.after(40, self._pump)

    def dispatch(self, action: str, payload) -> bool:
        """Apply one queued command. Returns True when asked to stop.

        Split out of `_pump` so tests drive the same dispatch the Tk
        thread does. Testing by calling `_do_*` directly proved worthless:
        commands could be dropped from the pump entirely and every test
        still passed.
        """
        if action == "show":
            self._do_show(payload)
        elif action == "hide":
            self._do_hide()
        elif action == "detail":
            self._do_detail(payload)
        elif action == "enabled":
            self._do_enabled(payload)
        elif action == "stop":
            return True
        return False

    # --- dragging ---------------------------------------------------------

    def _grab(self, event) -> None:
        self._drag_origin = (event.x_root, event.y_root)

    def _drag(self, event) -> None:
        root = self._root
        if root is None or self._drag_origin is None:
            return
        dx = event.x_root - self._drag_origin[0]
        dy = event.y_root - self._drag_origin[1]
        self._drag_origin = (event.x_root, event.y_root)
        root.geometry(f"+{root.winfo_x() + dx}+{root.winfo_y() + dy}")

    def _drop(self, event) -> None:
        self._drag_origin = None
        root = self._root
        if root is None:
            return
        x, y = root.winfo_x(), root.winfo_y()
        width = self.style.px(PANEL_WIDTH) + 2
        free = max(1, root.winfo_screenwidth() - width)
        fraction = (
            max(0.0, min(1.0, x / free)),
            max(0.0, min(1.0, y / max(1, root.winfo_screenheight()))),
        )
        # Remember it here as well as on disk. Hiding clears `_visible`,
        # so the next fight re-reads `_wanted` -- and without this the
        # window would snap back to wherever it was when the app started,
        # silently undoing the drag the user just made.
        self._wanted = fraction
        self._legacy = (None, None)
        if self._on_move is None:
            return
        try:
            self._on_move(*fraction)
        except Exception:
            pass

    # --- drawing ----------------------------------------------------------

    def _do_show(self, content: OverlayContent) -> None:
        root, canvas = self._root, self._canvas
        if root is None or canvas is None:
            return
        # Remembered even while disabled, so switching back on can repaint
        # the fight already in progress rather than waiting for the next.
        self._last_content = content
        if not self.enabled:
            return
        height = self._draw(canvas, content)
        # Width as well as height. Configuring only the height let the
        # canvas keep whatever width it was built with, which is how a
        # scaled panel ended up drawing past its own right edge.
        canvas.configure(width=self.style.px(PANEL_WIDTH), height=height)

        x, y = self._x(root, height + 2), self._y(root, height + 2)
        width = self.style.px(PANEL_WIDTH) + 2
        root.geometry(f"{width}x{height + 2}+{x}+{y}")

        if not self._visible:
            root.deiconify()
            # Re-assert topmost after deiconify; Windows drops the flag
            # when a fullscreen app takes the foreground in between.
            root.attributes("-topmost", True)
            self._visible = True

        self.place(root, x, y, width, height + 2)

    def place(self, root, x: int, y: int, width: int, height: int) -> tuple:
        """Move the window, then check it actually moved.

        Geometry set on a withdrawn `overrideredirect` window does not
        reliably survive mapping on Windows: the shell places it where it
        likes, and `winfo_x` then reports *that*, so every later show
        inherits the wrong position. Setting it again after the window
        exists fixes it -- but only if we look, so this looks.

        Records the outcome in `last_placement` for the log. A silent
        wrong answer here is what made this take three attempts to fix.
        """
        for attempt in range(3):
            try:
                root.update_idletasks()
                actual_x, actual_y = root.winfo_x(), root.winfo_y()
            except Exception:
                self.last_placement = (x, y, None, None, "unreadable")
                return (x, y)

            if abs(actual_x - x) <= 2 and abs(actual_y - y) <= 2:
                self.last_placement = (x, y, actual_x, actual_y, f"ok/{attempt}")
                return (actual_x, actual_y)

            try:
                root.geometry(f"{width}x{height}+{x}+{y}")
            except Exception:
                break

        self.last_placement = (x, y, actual_x, actual_y, "drifted")
        return (actual_x, actual_y)

    def _do_hide(self) -> None:
        if self._root is not None and self._visible:
            self._root.withdraw()
            self._visible = False

    def _do_detail(self, detail: str) -> None:
        """Apply a view change now, not at the next restart.

        Repainting the current boss is the whole point: a setting that
        only takes effect after a restart looks broken, and the user
        clicks it again, which puts it back where it started.
        """
        if detail == self.style.detail:
            return
        self.style.detail = detail
        if self._visible and self._last_content is not None:
            self._do_show(self._last_content)

    def _do_enabled(self, enabled: bool) -> None:
        self.enabled = enabled
        if not enabled:
            self._do_hide()
        elif self._last_content is not None:
            self._do_show(self._last_content)

    def _x(self, root, height: int | None = None) -> int:
        """Horizontal position, resolved from a fraction of free space.

        The stored value is not a pixel column, it is `x / (screen width -
        panel width)`. That makes 1.0 mean "flush with the right edge" on
        every display, and -- unlike a raw fraction of screen width -- it
        cannot put a 300px panel half off a 1366px laptop. Same reasoning
        as `FractionalRect` for the HUD regions.
        """
        if self._visible:
            return root.winfo_x()

        width = self.style.px(PANEL_WIDTH) + 2
        free = max(1, root.winfo_screenwidth() - width)
        fraction = self._wanted[0]
        if fraction is None:
            fraction = self._migrate_x(root, free)
        if fraction is None:
            # Default corner: near the right edge, but not touching it.
            margin = self.style.px(24)
            return max(0, root.winfo_screenwidth() - width - margin)
        return max(0, min(free, int(round(fraction * free))))

    def _y(self, root, height: int | None = None) -> int:
        """Vertical position as a fraction of screen height.

        Clamped against the panel's *current* height rather than a fixed
        one, because a boss with more to say makes a taller panel and a
        position that fits Agheel could hang off the bottom for Malenia.
        """
        if self._visible:
            return root.winfo_y()

        screen = root.winfo_screenheight()
        panel = height if height is not None else self.style.px(120)
        limit = max(0, screen - panel)
        fraction = self._wanted[1]
        if fraction is None:
            fraction = self._migrate_y(root, screen)
        if fraction is None:
            return min(self.style.px(24), limit)
        return max(0, min(limit, int(round(fraction * screen))))

    # --- one-time migration from the old pixel positions ------------------

    def _migrate_x(self, root, free: int) -> float | None:
        """Convert a legacy pixel column into a fraction, once.

        Early builds stored raw pixels. Those are meaningless on a
        different display, but discarding them would silently move the
        window for anyone upgrading, so they are converted the first time
        the screen size is known.
        """
        pixels = self._legacy[0]
        if pixels is None:
            return None
        fraction = max(0.0, min(1.0, pixels / free))
        self._wanted = (fraction, self._wanted[1])
        return fraction

    def _migrate_y(self, root, screen: int) -> float | None:
        pixels = self._legacy[1]
        if pixels is None:
            return None
        fraction = max(0.0, min(1.0, pixels / max(1, screen)))
        self._wanted = (self._wanted[0], fraction)
        return fraction

    def _draw(self, canvas, content: OverlayContent) -> int:
        style = self.style
        canvas.delete("all")
        width = style.px(PANEL_WIDTH)
        left = style.px(PADDING)
        right = width - style.px(PADDING)
        y = style.px(PADDING)

        title = canvas.create_text(
            left, y, anchor="nw", text=content.name.upper(),
            fill=TITLE_FG, font=style.font(TITLE_FONT, "bold"),
            width=right - left,
        )
        y += _measure(
            canvas, title,
            style.px(TITLE_HEIGHT) * _line_count(content.name, 22),
        )
        y += style.px(AFTER_TITLE)

        if style.detail == FULL:
            damage, statuses = content.damage, content.statuses
        else:
            damage = content.damage_highlights()
            statuses = content.status_highlights()

        # Drawn only when it is the sole thing to say. Otherwise it is a
        # prose restatement of the two sections directly beneath it, and
        # it cost two lines at the top of a panel meant to be read in a
        # glance mid-fight.
        if SHOW_HEADLINE or not (damage or statuses):
            headline = canvas.create_text(
                left, y, anchor="nw", text=content.headline,
                fill=HEADLINE_FG, font=style.font(HEADLINE_FONT),
                width=right - left,
            )
            y += _measure(
                canvas, headline,
                style.px(HEADLINE_HEIGHT) * _line_count(content.headline, 38),
            )
            y += style.px(AFTER_HEADLINE)

        # A section with no rows is a heading over nothing. Seven bosses
        # have completely flat damage -- every type identical -- so there
        # is genuinely nothing to recommend, and "DAMAGE" followed by
        # blank space reads as a rendering fault rather than as an answer.
        # The summary line already says "no notable weaknesses".
        best = content.best_damage
        drew = False
        if damage:
            y = self._section(
                canvas, "DAMAGE", damage, y, left, right,
                highlight=best.key if best is not None else None,
            )
            drew = True
        if statuses:
            if drew:
                y += style.px(BETWEEN_SECTIONS)
            y = self._section(canvas, "STATUS", statuses, y, left, right)

        if content.poise is not None:
            y += style.px(BEFORE_POISE)
            canvas.create_text(
                left, y, anchor="nw", text=f"Poise  {content.poise}",
                fill=LABEL_FG, font=style.font(POISE_FONT),
            )
            y += style.px(HEADLINE_HEIGHT)

        return y + style.px(PADDING)

    def _section(self, canvas, title, rows, y, left, right, highlight=None) -> int:
        style = self.style
        canvas.create_text(
            left, y, anchor="nw", text=title,
            fill=SECTION_FG, font=style.font(SECTION_FONT, "bold"),
        )
        y += style.px(SECTION_HEADER)

        for row in rows:
            colour = UNKNOWN_FG if not row.known else EFFECT_FG[row.tone]
            recommended = highlight is not None and row.key == highlight
            if not row.known:
                label_fg = UNKNOWN_FG
            elif recommended:
                label_fg = SECTION_FG
            else:
                label_fg = LABEL_FG
            canvas.create_text(
                left, y, anchor="nw",
                # The chevron survives where colour does not: a bold gold
                # label is invisible to a red-green colourblind player, and
                # this is the one row that has to be findable at a glance.
                text=("▸ " + row.label) if recommended else row.label,
                fill=label_fg,
                font=style.font(
                    ROW_FONT,
                    "bold" if (row.is_notable or recommended) else "normal",
                ),
            )

            bar_right = right - style.px(62)
            bar_left = bar_right - style.px(BAR_WIDTH)
            bar_top = y + style.px(5)
            canvas.create_rectangle(
                bar_left, bar_top,
                bar_right, bar_top + style.px(BAR_HEIGHT),
                fill=TRACK, outline="",
            )
            if row.known and row.weight > 0:
                canvas.create_rectangle(
                    bar_left, bar_top,
                    bar_left + style.px(BAR_WIDTH * row.weight),
                    bar_top + style.px(BAR_HEIGHT),
                    fill=colour, outline="",
                )

            canvas.create_text(
                right, y, anchor="ne",
                text=row.description if row.known else UNKNOWN_TEXT,
                fill=colour,
                font=style.font(ROW_FONT,
                                "bold" if row.is_notable else "normal"),
            )
            y += style.px(ROW_HEIGHT)
        return y


def _measure(canvas, item, fallback: int) -> int:
    """The height a wrapped text item actually occupies.

    Character-count estimates cannot get this right. Segoe UI is
    proportional, uppercase is far wider than lowercase, and Tk wraps on
    word boundaries -- so "SIR GIDEON OFNIR, THE ALL-KNOWING" takes a
    different number of lines than its length suggests, and the summary
    line underneath landed on top of it.

    Tk already knows the answer, so ask: `bbox` returns the real extent of
    the rendered item. `fallback` is only for canvases that cannot measure,
    which in practice means the test double.
    """
    try:
        box = canvas.bbox(item)
    except Exception:
        return fallback
    if not box:
        return fallback
    return max(1, box[3] - box[1])


def _line_count(text: str, per_line: int) -> int:
    """Rough line count, used only when the canvas cannot measure.

    Erring high costs a few pixels of padding; erring low overlaps the
    next row, so the rounding is deliberately generous.
    """
    return max(1, -(-len(text) // per_line))


def build_overlay(
    config,
    *,
    on_move: Callable[[int, int], None] | None = None,
):
    """The overlay the current settings and machine allow.

    Never raises, and never returns None -- callers get something with the
    right methods either way.
    """
    # Built even when the overlay is switched off, so the tray toggle can
    # turn it back on without a restart. An idle Tk mainloop costs
    # nothing; a setting that needs a restart costs a bug report.
    try:
        import tkinter  # noqa: F401
    except Exception as exc:
        return NullOverlay(f"tkinter is not available: {exc}")

    try:
        window = OverlayWindow(
            style=OverlayStyle(
                scale=getattr(config, "overlay_scale", 1.0),
                opacity=getattr(config, "overlay_opacity", 0.88),
                detail=getattr(config, "overlay_detail", COMPACT),
            ),
            position=(
                getattr(config, "overlay_fx", None),
                getattr(config, "overlay_fy", None),
            ),
            legacy_pixels=(
                getattr(config, "overlay_x", None),
                getattr(config, "overlay_y", None),
            ),
            on_move=on_move,
            enabled=bool(getattr(config, "overlay_enabled", True)),
        )
        window.start()
    except Exception as exc:
        return NullOverlay(f"{type(exc).__name__}: {exc}")

    # `start` gives Tk five seconds to produce a root. If it did not, the
    # window would silently swallow every command from here on.
    if window._root is None:
        return NullOverlay(
            window.start_error or "Tk produced no window within 5s"
        )
    return window
