"""Ensure the terminal remains usable at compact sizes without a real ChatGPT login."""

import asyncio
import unittest
from unittest.mock import patch

from prompt_toolkit.application.current import set_app
from prompt_toolkit.data_structures import Size
from prompt_toolkit.output import DummyOutput

from chatgpt_tui import ChatTui


class SizedOutput(DummyOutput):
    def __init__(self, rows, columns):
        self.size = Size(rows=rows, columns=columns)

    def get_size(self):
        return self.size


def make_tui():
    return ChatTui(
        lambda: [],
        lambda _cid: {"id": _cid, "messages": []},
        lambda *args: {},
    )


class CompactTuiTests(unittest.TestCase):
    def test_layout_uses_actual_rows(self):
        tui = make_tui()
        for rows in (1, 2, 3, 6, 7, 9, 12, 24):
            with self.subTest(rows=rows), patch.object(
                tui.app.output, "get_size",
                return_value=Size(rows=rows, columns=80),
            ):
                self.assertEqual(tui.viewport_height().preferred, rows)
                self.assertEqual(bool(tui.full_footer_filter()), rows >= 7)
                tui.resume_visible = True
                self.assertEqual(bool(tui.resume_filter()), rows >= 9)
                tui.resume_visible = False

    def test_picker_shortcut_ignores_undersized_terminal(self):
        tui = make_tui()
        for rows, columns in ((2, 80), (8, 80), (12, 20)):
            with self.subTest(size=(rows, columns)), patch.object(
                tui.app.output, "get_size",
                return_value=Size(rows=rows, columns=columns),
            ):
                tui.open_resume()
                self.assertFalse(tui.resume_visible)
                self.assertIn("Enlarge the terminal", tui.status)

    def test_screen_renderer_never_draws_window_too_small(self):
        async def draw(rows, columns, resume):
            tui = make_tui()
            output = SizedOutput(rows, columns)
            tui.app.output = output
            tui.app.renderer.output = output
            tui.resume_visible = resume
            with set_app(tui.app):
                tui.app.renderer.render(tui.app, tui.app.layout)
                await asyncio.sleep(0)
            screen = tui.app.renderer._last_screen
            image = "\n".join(
                "".join(cell.char for _col, cell in
                        sorted(screen.data_buffer.get(row, {}).items()))
                for row in range(rows)
            )
            self.assertNotIn(
                "Window too small", image, f"{rows}x{columns}; resume={resume}"
            )
            self.assertTrue(screen.data_buffer, f"Empty screen at {rows}x{columns}")
            return image

        async def all_cases():
            for rows, columns, resume in (
                (1, 20, False), (2, 20, False), (3, 25, False),
                (5, 40, False), (6, 50, False), (7, 50, False),
                (8, 60, False), (9, 60, False), (12, 80, True),
                (16, 80, True), (24, 100, True),
            ):
                with self.subTest(size=(rows, columns), resume=resume):
                    await draw(rows, columns, resume)

        asyncio.run(all_cases())


if __name__ == "__main__":
    unittest.main()
