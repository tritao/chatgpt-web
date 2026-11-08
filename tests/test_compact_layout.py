"""Compact terminal geometry stays usable without a six-row minimum."""

from __future__ import annotations

import asyncio
from contextlib import redirect_stdout
from io import StringIO
import unittest
from unittest.mock import patch

from prompt_toolkit.data_structures import Size

from chatgpt_tui import ChatTui


class CompactLayoutTests(unittest.TestCase):
    @staticmethod
    def make_ui():
        return ChatTui(
            list_conversations=lambda: [],
            load_conversation=lambda _id: {},
            send_prompt=lambda *args, **kwargs: {},
        )

    def test_responsive_dimensions_and_compact_footer(self):
        with redirect_stdout(StringIO()):
            ui = self.make_ui()
        for rows in (1, 2, 3, 5, 6):
            with self.subTest(rows=rows), patch.object(
                ui.app.output, "get_size", return_value=Size(rows, 40)
            ):
                self.assertEqual(ui.viewport_height().preferred, rows)
                self.assertFalse(ui.full_footer_filter())
        with patch.object(ui.app.output, "get_size", return_value=Size(20, 80)):
            self.assertTrue(ui.full_footer_filter())

    def test_too_small_for_picker_preserves_composer_focus(self):
        with redirect_stdout(StringIO()):
            ui = self.make_ui()
        for size in (Size(2, 50), Size(10, 20)):
            with self.subTest(size=size), patch.object(
                ui.app.output, "get_size", return_value=size
            ):
                ui.open_resume()
                self.assertFalse(ui.resume_visible)
                self.assertEqual(ui.status, "Enlarge the terminal to browse conversations")
                self.assertIs(ui.app.layout.current_control, ui.input.control)

    def test_resizing_while_picker_open_restores_input_focus(self):
        with redirect_stdout(StringIO()):
            ui = self.make_ui()
        with patch.object(ui.app.output, "get_size", return_value=Size(24, 80)):
            ui.resume_visible = True
            ui.app.layout.focus(ui.resume_search)
            ui.detect_terminal_resize(ui.app)
        with patch.object(ui.app.output, "get_size", return_value=Size(3, 20)):
            ui.detect_terminal_resize(ui.app)
        self.assertFalse(ui.resume_visible)
        self.assertEqual(ui.status, "Enlarge the terminal to browse conversations")
        self.assertIs(ui.app.layout.current_control, ui.input.control)

    def test_renderer_does_not_substitute_window_too_small(self):
        async def render_sizes():
            output = StringIO()
            with redirect_stdout(output):
                ui = self.make_ui()
                for rows, columns in (
                    (1, 20), (2, 30), (3, 50), (5, 60),
                    (6, 80), (7, 80), (9, 24), (24, 100),
                ):
                    with patch.object(
                        ui.app.output, "get_size", return_value=Size(rows, columns)
                    ):
                        ui.app.renderer.render(ui.app, ui.app.layout, is_done=False)
                        self.assertEqual(ui.app.renderer._last_screen.height, rows)
                await asyncio.sleep(0)
            rendered = output.getvalue()
            self.assertNotIn("Window too small", rendered)
            self.assertIn("Ask ChatGPT", rendered)

        asyncio.run(render_sizes())


if __name__ == "__main__":
    unittest.main()
