"""Fullscreen terminal interface for chatgpt-web."""

from __future__ import annotations

import asyncio
from io import StringIO
import base64
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from typing import Any, Callable

from prompt_toolkit.application import Application, run_in_terminal
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import ANSI, FormattedText
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Float, FloatContainer, HSplit, Layout, Window
from prompt_toolkit.layout.containers import ConditionalContainer
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import D
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.layout.processors import Processor, Transformation
from prompt_toolkit.styles import Style
from prompt_toolkit.data_structures import Point
from prompt_toolkit.widgets import Frame, TextArea
from rich.console import Console
from rich.markdown import BlockQuote, CodeBlock, Markdown
from rich.padding import Padding
from rich.rule import Rule
from rich.segment import Segment
from rich.style import Style as RichStyle
from rich.syntax import Syntax
from rich.text import Text
from rich.theme import Theme


Message = dict[str, str]
LoadConversation = Callable[[str], dict[str, Any]]
ListConversations = Callable[[], list[dict[str, Any]]]
SendPrompt = Callable[
    [
        str | None,
        str,
        list[dict[str, Any]],
        Callable[[dict[str, Any]], None],
        Callable[[Any], None],
    ],
    dict[str, Any],
]
UploadImage = Callable[[bytes, str, str, int, int], dict[str, Any]]
RenameConversation = Callable[[str, str], None]

ANNOTATION_PATTERN = re.compile(r"\ue200([^\ue201\ue202]+)(?:\ue202(.*?))?\ue201")
WRITING_DIRECTIVE_PATTERN = re.compile(
    r'(?m)^[\ue200]?\s*:::writing\{(?P<attributes>[^}\n]*)\}\s*$'
)

MARKDOWN_THEME = Theme({
    "markdown.text": "#e5e7eb",
    "markdown.paragraph": "#e5e7eb",
    "markdown.h1": "bold #f3f4f6",
    "markdown.h2": "bold #f3f4f6",
    "markdown.h3": "bold #f3f4f6",
    "markdown.h4": "bold #e5e7eb",
    "markdown.h5": "bold #d1d5db",
    "markdown.h6": "bold #d1d5db",
    "markdown.link": "underline #7dd3fc",
    "markdown.link_url": "underline #7dd3fc",
    "markdown.code": "bold #fbbf24",
    "markdown.block_quote": "#9ca3af",
    "markdown.hr": "#606060",
    "markdown.list": "#e5e7eb",
    "markdown.item.bullet": "bold #86efac",
    "markdown.item.number": "bold #86efac",
})


def detect_code_language(code: str) -> str:
    """Choose a useful lexer for common unlabelled Markdown code blocks."""
    stripped = code.lstrip()
    if re.search(r"^(?:from\s+\w+\s+import|import\s+\w+|def\s+\w+|class\s+\w+)", stripped, re.M):
        return "python"
    if re.search(r"^(?:use\s+(?:std|crate)::|(?:pub\s+)?fn\s+\w+|impl(?:<.*?>)?\s+\w+)", stripped, re.M):
        return "rust"
    if re.search(r"\b(?:const|let|var)\s+\w+\s*=|=>|console\.\w+\(", code):
        return "javascript"
    if re.search(r"^\s*(?:SELECT|INSERT|UPDATE|DELETE|CREATE TABLE)\b", code, re.I | re.M):
        return "sql"
    if stripped.startswith(("#!/bin/sh", "#!/usr/bin/env bash", "#!/bin/bash")):
        return "bash"
    if stripped.startswith(("{", "[")):
        try:
            import json

            json.loads(stripped)
            return "json"
        except ValueError:
            pass
    if re.search(r"</?[A-Za-z][^>]*>", code):
        return "html"
    if re.search(r"^\s*#include\s*[<\"]|\b(?:int|void|char)\s+\w+\s*\([^)]*\)\s*\{", code, re.M):
        return "c"
    return "text"


class HighlightedCodeBlock(CodeBlock):
    def __rich_console__(self, console: Console, options: Any) -> Any:
        code = str(self.text).rstrip()
        lexer = self.lexer_name
        if lexer == "text":
            lexer = detect_code_language(code)
        yield Syntax(code, lexer, theme=self.theme, word_wrap=True, padding=1)


class CompactBlockQuote(BlockQuote):
    """Render quotes as a narrow rail instead of a full-width block."""

    def __rich_console__(self, console: Console, options: Any) -> Any:
        quote_options = options.update(width=max(1, options.max_width - 4))
        lines = console.render_lines(self.elements, quote_options, style=self.style)
        rail = Segment("│ ", RichStyle(color="#6b7280", dim=True))
        for line in lines:
            yield rail
            yield from line
            yield Segment("\n")


class ChatMarkdown(Markdown):
    elements = {
        **Markdown.elements,
        "blockquote_open": CompactBlockQuote,
        "fence": HighlightedCodeBlock,
        "code_block": HighlightedCodeBlock,
    }


class PromptMarkdown:
    """Markdown content with a Codex-style prompt marker on its first line."""

    def __init__(self, text: str) -> None:
        self.text = text

    def __rich_console__(self, console: Console, options: Any) -> Any:
        content_options = options.update(width=max(1, options.max_width - 2))
        lines = console.render_lines(
            ChatMarkdown(self.text, code_theme="monokai"),
            content_options,
            pad=False,
        )
        for index, line in enumerate(lines):
            marker = "› " if index == 0 else "  "
            style = RichStyle(color="#67e8f9", bold=True) if index == 0 else None
            yield Segment(marker, style)
            yield from line
            yield Segment("\n")


class SlashCommandCompleter(Completer):
    COMMANDS = (
        ("/new", "start a new conversation"),
        ("/resume", "search recent conversations"),
        ("/history", "reload this conversation"),
        ("/rename", "rename this conversation"),
        ("/copy", "copy the latest response"),
        ("/remove", "remove the latest image"),
        ("/clear", "clear the displayed transcript"),
        ("/help", "show available commands"),
        ("/exit", "quit chatgpt-web"),
        ("/quit", "quit chatgpt-web"),
    )

    def get_completions(self, document: Any, complete_event: Any) -> Any:
        token = document.text_before_cursor
        if not token.startswith("/") or any(char.isspace() for char in token):
            return
        query = token.casefold()
        for command, description in self.COMMANDS:
            if command.startswith(query) and command != query:
                yield Completion(
                    command,
                    start_position=-len(token),
                    display=command,
                    display_meta=description,
                )


class ComposerPlaceholderProcessor(Processor):
    def apply_transformation(self, transformation_input: Any) -> Transformation:
        fragments = transformation_input.fragments
        if transformation_input.document.text or transformation_input.lineno != 0:
            return Transformation(fragments)
        return Transformation(
            fragments + [("class:placeholder", "Ask ChatGPT anything")],
            source_to_display=lambda position: position,
            display_to_source=lambda position: 0,
        )


class TranscriptWindow(Window):
    """A Window with the multi-line wheel movement users expect in terminals."""

    def __init__(self, *args: Any, on_manual_scroll: Callable[[], None], **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.on_manual_scroll = on_manual_scroll

    def _scroll_up(self) -> None:
        info = self.render_info
        if info is None:
            return
        self.on_manual_scroll()
        self.vertical_scroll = max(0, info.vertical_scroll - 4)

    def _scroll_down(self) -> None:
        info = self.render_info
        if info is None:
            return
        self.on_manual_scroll()
        last_scroll = max(0, info.content_height - info.window_height)
        self.vertical_scroll = min(last_scroll, info.vertical_scroll + 4)


def render_chatgpt_annotations(text: str) -> str:
    """Turn ChatGPT web's private-use annotations into terminal-safe text."""

    writing_directive = WRITING_DIRECTIVE_PATTERN.search(text)
    if writing_directive:
        attributes = writing_directive.group("attributes")
        title_match = re.search(r'\btitle="([^"]+)"', attributes)
        title = title_match.group(1) if title_match else "Document"
        text = WRITING_DIRECTIVE_PATTERN.sub(f"**{title}**", text, count=1)
        # The web client treats the matching container delimiter as metadata.
        text = re.sub(r"(?m)^\s*:::\s*$", "", text)

    def replace(match: re.Match[str]) -> str:
        kind = match.group(1).casefold()
        fields = (match.group(2) or "").split("\ue202")
        if kind == "filecite":
            reference = next((field for field in fields if "file" in field), "")
            location = next((field for field in fields if re.fullmatch(
                r"L\d+(?:-L\d+)?", field
            )), "")
            file_match = re.search(r"file(\d+)", reference)
            file_label = f"F{int(file_match.group(1)) + 1}" if file_match else "file"
            location = location.replace("-", "–")
            detail = f"{file_label}:{location}" if location else file_label
            return f"[{detail}]"
        if kind == "cite":
            return "[source]"
        # Image groups, navigation lists, entities, and other web-only widgets
        # have no useful terminal payload in the marker itself.
        return ""

    rendered = ANNOTATION_PATTERN.sub(replace, text)
    # Some ChatGPT Web conversation nodes escape every Markdown delimiter in
    # otherwise complete Markdown. Only unescape after detecting a fenced block
    # so intentional backslashes in ordinary prose and source code survive.
    if re.search(r"(?m)^\s*\\`\\`\\`", rendered):
        rendered = re.sub(r"\\([`*_{}\[\]()#+.!>|~-])", r"\1", rendered)
    # Streaming can end between the opening marker and its terminator. Hide the
    # incomplete suffix until the next delta completes it.
    rendered = re.sub(r"\ue200[^\ue201]*$", "", rendered)
    rendered = rendered.replace("\ue200", "").replace("\ue201", "").replace("\ue202", "")
    rendered = re.sub(r"[ \t]+([,.;:!?])", r"\1", rendered)
    return rendered


def visible_chat_message(message: dict[str, Any]) -> Message | None:
    """Return user-facing turns while excluding web tool protocol nodes."""
    role = message.get("role")
    text = message.get("text")
    content_type = message.get("content_type")
    if role == "user":
        visible = content_type in (None, "text")
    elif role == "assistant":
        visible = (
            content_type in (None, "text")
            and message.get("channel") in (None, "commentary", "final")
            and message.get("recipient") in (None, "all")
        )
    else:
        visible = False
    if not visible or not isinstance(text, str) or not text:
        return None
    return {"role": role, "text": text}


class ChatTui:
    def __init__(
        self,
        list_conversations: ListConversations,
        load_conversation: LoadConversation,
        send_prompt: SendPrompt,
        conversation_id: str | None = None,
        title: str = "New conversation",
        initial_messages: list[dict[str, Any]] | None = None,
        model: str | None = None,
        upload_image: UploadImage | None = None,
        rename_conversation: RenameConversation | None = None,
    ) -> None:
        self.list_conversations = list_conversations
        self.load_conversation = load_conversation
        self.send_prompt = send_prompt
        self.upload_image = upload_image
        self.rename_conversation = rename_conversation
        self.conversation_id = conversation_id
        self.title = title
        self.model = model or os.environ.get("CHATGPT_WEB_MODEL", "gpt-5-6-thinking")
        self.messages: list[Message] = []
        for message in initial_messages or []:
            visible = visible_chat_message(message)
            if visible is not None:
                self.messages.append(visible)
        self.busy = False
        self.loading_conversation = False
        self.loading_started: float | None = None
        self.load_on_start = conversation_id is not None and not initial_messages
        self.uploading_image = False
        self.upload_started: float | None = None
        self.pending_attachments: list[dict[str, Any]] = []
        self.status = "Ready"
        self.cancel_connection: Any = None
        self.lock = threading.RLock()
        self.resume_visible = False
        self.resume_loading = False
        self.resume_items: list[dict[str, Any]] = []
        self.resume_filtered: list[dict[str, Any]] = []
        self.resume_index = 0
        self.resume_filter = Condition(lambda: self.resume_visible)
        self.follow_output = True
        self.transcript_line_count = 1
        self.active_started: float | None = None
        self.last_elapsed: float | None = None
        self.render_cache: dict[tuple[str, str, int, bool], str] = {}
        self.formatted_transcript_source = ""
        self.formatted_transcript: ANSI = ANSI("")
        self.committed_message_count = 0
        self.history_tail = ""
        self.event_loop: asyncio.AbstractEventLoop | None = None
        self.last_terminal_size: tuple[int, int] | None = None
        self.resize_reflowing = False
        self.resize_reflow_pending = False

        self.transcript_control = FormattedTextControl(
            text=self.render_formatted_transcript,
            focusable=True,
            get_cursor_position=self.transcript_cursor_position,
        )
        self.transcript_window = TranscriptWindow(
            content=self.transcript_control,
            wrap_lines=True,
            right_margins=[],
            allow_scroll_beyond_bottom=False,
            on_manual_scroll=self.pause_following,
        )
        self.input = TextArea(
            height=D(min=1, max=6),
            multiline=True,
            wrap_lines=True,
            prompt=self.render_prompt,
            dont_extend_height=True,
            completer=SlashCommandCompleter(),
            complete_while_typing=True,
            input_processors=[ComposerPlaceholderProcessor()],
            style="class:composer",
        )
        self.resume_search = TextArea(
            height=1,
            multiline=False,
            prompt="Search: ",
        )
        self.resume_search.buffer.on_text_changed += lambda _buffer: self.filter_resume()
        self.resume_list_control = FormattedTextControl(
            text=self.render_resume_list,
            focusable=True,
        )
        self.resume_list_window = Window(
            content=self.resume_list_control,
            wrap_lines=False,
            always_hide_cursor=True,
        )
        self.bindings = self.make_bindings()
        base = HSplit(
            [
                self.transcript_window,
                Window(
                    height=1,
                    content=FormattedTextControl(self.render_activity),
                    style="class:activity",
                ),
                Window(height=1, char=" ", style="class:composer"),
                self.input,
                Window(height=1, char=" ", style="class:composer"),
                Window(
                    height=1,
                    content=FormattedTextControl(self.render_metadata),
                    style="class:metadata",
                ),
            ],
            height=self.viewport_height,
        )
        resume_dialog = ConditionalContainer(
            content=Frame(
                HSplit([
                    self.resume_search,
                    Window(height=1, char="─", style="class:separator"),
                    self.resume_list_window,
                    Window(
                        height=1,
                        content=FormattedTextControl(
                            " ↑/↓ select  Enter open  Esc close "
                        ),
                        style="class:help",
                    ),
                ]),
                title="Resume conversation",
            ),
            filter=self.resume_filter,
        )
        root = FloatContainer(
            content=base,
            floats=[
                Float(
                    xcursor=True,
                    ycursor=True,
                    attach_to_window=self.input.window,
                    allow_cover_cursor=True,
                    content=CompletionsMenu(max_height=8, scroll_offset=1),
                ),
                Float(
                    content=resume_dialog,
                    left=4,
                    right=4,
                    top=2,
                    bottom=2,
                ),
            ],
        )
        self.app: Application[Any] = Application(
            layout=Layout(root, focused_element=self.input),
            key_bindings=self.bindings,
            full_screen=False,
            mouse_support=True,
            min_redraw_interval=0.05,
            max_render_postpone_time=0.1,
            refresh_interval=0.12,
            before_render=self.detect_terminal_resize,
            style=Style.from_dict({
                "separator": "#6b7280",
                "activity": "#9ca3af",
                "composer": "bg:#4b4b4b #f9fafb",
                "prompt": "bg:#4b4b4b bold #f9fafb",
                "placeholder": "bg:#4b4b4b #9ca3af",
                "metadata": "bg:#272727 #9ca3af",
                "metadata.model": "bg:#272727 #fbbf24",
                "metadata.ready": "bg:#272727 #86efac",
                "help": "bg:#1f2937 #9ca3af",
                "frame.label": "#5eead4",
                "frame.border": "#4b5563",
                "resume.selected": "bg:#0f766e #ffffff bold",
                "resume.item": "#d1d5db",
                "resume.time": "#9ca3af",
                "completion-menu": "bg:#303030 #e5e7eb",
                "completion-menu.completion": "bg:#303030 #e5e7eb",
                "completion-menu.completion.current": "bg:#0f766e #ffffff bold",
                "completion-menu.meta.completion": "bg:#303030 #9ca3af",
                "completion-menu.meta.completion.current": "bg:#0f766e #ccfbf1",
            }),
        )

    def render_prompt(self) -> FormattedText:
        return FormattedText([("class:prompt", " › ")])

    def viewport_height(self) -> D:
        try:
            rows = self.app.output.get_size().rows
        except Exception:
            rows = shutil.get_terminal_size((100, 24)).lines
        return D.exact(max(6, rows))

    @staticmethod
    def format_elapsed(seconds: float) -> str:
        total = max(0, int(seconds))
        if total < 60:
            return f"{total}s"
        return f"{total // 60}m {total % 60:02d}s"

    def render_activity(self) -> FormattedText:
        started = (
            self.active_started if self.busy
            else self.loading_started if self.loading_conversation
            else self.upload_started
        )
        if started is not None:
            frames = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
            frame = frames[int((time.monotonic() - started) * 10) % len(frames)]
            elapsed = self.format_elapsed(time.monotonic() - started)
            return FormattedText([("", f" {frame} {self.status} · {elapsed} ")])
        if self.status not in ("Ready", ""):
            return FormattedText([("", f" • {self.status} ")])
        if self.pending_attachments:
            names = ", ".join(item["name"] for item in self.pending_attachments)
            return FormattedText([("", f" 📎 {names} · /remove to detach ")])
        if self.last_elapsed is not None:
            return FormattedText([
                ("", f" — Worked for {self.format_elapsed(self.last_elapsed)} "),
            ])
        return FormattedText([("", "")])

    def render_metadata(self) -> FormattedText:
        cwd = str(Path.cwd())
        home = str(Path.home())
        if cwd == home or cwd.startswith(home + os.sep):
            cwd = "~" + cwd[len(home):]
        state = (
            "streaming" if self.busy else
            "loading" if self.loading_conversation else
            "uploading" if self.uploading_image else
            "ready"
        )
        title = self.title if len(self.title) <= 42 else self.title[:39] + "…"
        return FormattedText([
            ("class:metadata.model", f" {self.model} "),
            ("class:metadata.ready", f"{state} "),
            ("class:metadata", f"· {title} · {cwd} "),
        ])

    def render_transcript(self) -> str:
        with self.lock:
            messages = [
                dict(message)
                for message in self.messages[self.committed_message_count:]
            ]
        width = self.transcript_width()
        if not messages:
            if self.committed_message_count:
                self.transcript_line_count = self.history_tail.count("\n") + 1
                return self.history_tail
            buffer = StringIO()
            console = Console(
                file=buffer,
                force_terminal=True,
                color_system="truecolor",
                no_color=False,
                width=width,
                theme=MARKDOWN_THEME,
            )
            console.print(Text("Start a conversation below.", style="dim"))
            rendered = buffer.getvalue()
        else:
            pieces = [self.history_tail] if self.history_tail else []
        for index, message in enumerate(messages):
            role = message["role"]
            text = render_chatgpt_annotations(message["text"])
            active = self.busy and index == len(messages) - 1 and role == "assistant"
            separated = index > 0 or bool(self.history_tail)
            key = (role, text, width, separated)
            if active or key not in self.render_cache:
                value = self.render_message(role, text, width, separated, active)
                if not active:
                    if len(self.render_cache) > 256:
                        self.render_cache.clear()
                    self.render_cache[key] = value
            else:
                value = self.render_cache[key]
            pieces.append(value)
        if messages:
            rendered = "".join(pieces)
        self.transcript_line_count = rendered.count("\n") + 1
        return rendered

    def transcript_width(self) -> int:
        try:
            return max(40, self.app.output.get_size().columns - 3)
        except Exception:
            return max(40, shutil.get_terminal_size((100, 24)).columns - 3)

    def detect_terminal_resize(self, _app: Application[Any]) -> None:
        """Replay committed history when terminal dimensions change."""
        try:
            size = self.app.output.get_size()
            current = (size.columns, size.rows)
        except Exception:
            fallback = shutil.get_terminal_size((100, 24))
            current = (fallback.columns, fallback.lines)
        if self.last_terminal_size is None:
            self.last_terminal_size = current
            return
        if current == self.last_terminal_size:
            return
        self.last_terminal_size = current
        if self.resize_reflowing:
            self.resize_reflow_pending = True
            return
        if self.event_loop is not None:
            self.event_loop.call_soon(self.reflow_committed_history)

    def reflow_committed_history(self) -> None:
        if self.resize_reflowing:
            self.resize_reflow_pending = True
            return
        self.resize_reflowing = True
        with self.lock:
            completed = [
                dict(message)
                for message in self.messages[:self.committed_message_count]
            ]
        rendered = self.render_history(completed) if completed else ""
        committed = self.partition_history(rendered)
        self.formatted_transcript_source = ""

        def replay() -> None:
            self.reset_terminal_session()
            self.write_terminal_history(committed)

        future = run_in_terminal(replay, render_cli_done=False)

        def finished(_future: Any) -> None:
            self.resize_reflowing = False
            self.scroll_bottom()
            if self.resize_reflow_pending:
                self.resize_reflow_pending = False
                if self.event_loop is not None:
                    self.event_loop.call_soon(self.reflow_committed_history)

        future.add_done_callback(finished)

    def render_history(self, messages: list[Message]) -> str:
        width = self.transcript_width()
        return "".join(
            self.render_message(
                message["role"],
                render_chatgpt_annotations(message["text"]),
                width,
                index > 0,
                False,
            )
            for index, message in enumerate(messages)
        )

    @staticmethod
    def write_terminal_history(rendered: str) -> None:
        sys.stdout.write(rendered)
        sys.stdout.flush()

    @staticmethod
    def reset_terminal_session() -> None:
        # Clear the visible screen and purge earlier terminal scrollback while
        # staying on the main screen. This makes launch the history boundary.
        sys.stdout.write("\x1b[2J\x1b[3J\x1b[H")
        sys.stdout.flush()

    def partition_history(self, rendered: str) -> str:
        """Keep the newest viewport live and return older lines to commit."""
        transcript_rows = max(1, self.viewport_height().preferred - 5)
        lines = rendered.splitlines(keepends=True)
        split = max(0, len(lines) - transcript_rows)
        self.history_tail = "".join(lines[split:])
        return "".join(lines[:split])

    def commit_initial_history(self) -> None:
        with self.lock:
            messages = [dict(message) for message in self.messages]
        if not messages:
            return
        rendered = self.render_history(messages)
        committed = self.partition_history(rendered)
        self.write_terminal_history(committed)
        self.committed_message_count = len(messages)

    def commit_loaded_history(self, messages: list[Message]) -> None:
        rendered = self.render_history(messages)
        committed = self.partition_history(rendered)
        self.committed_message_count = len(messages)

        def schedule_write() -> None:
            future = run_in_terminal(
                lambda: self.write_terminal_history(committed),
                render_cli_done=False,
            )

            def finished(_future: Any) -> None:
                self.loading_conversation = False
                self.loading_started = None
                self.status = "Ready"
                self.scroll_bottom()

            future.add_done_callback(finished)

        if self.event_loop is not None:
            self.event_loop.call_soon_threadsafe(schedule_write)

    def commit_completed_turns(self) -> None:
        with self.lock:
            end = len(self.messages)
            messages = [
                dict(message)
                for message in self.messages[self.committed_message_count:end]
            ]
            # Remove completed turns from the live layout immediately. Rich
            # rendering and terminal output happen off the input event loop.
            self.committed_message_count = end
        if not messages:
            return
        rendered = self.render_history(messages)
        committed = self.partition_history(self.history_tail + rendered)

        def schedule_write() -> None:
            future = run_in_terminal(
                lambda: self.write_terminal_history(committed),
                render_cli_done=False,
            )
            future.add_done_callback(lambda _future: self.scroll_bottom())

        if self.event_loop is not None:
            self.event_loop.call_soon_threadsafe(schedule_write)

    def render_formatted_transcript(self) -> ANSI:
        rendered = self.render_transcript()
        if rendered != self.formatted_transcript_source:
            self.formatted_transcript_source = rendered
            self.formatted_transcript = ANSI(rendered)
        return self.formatted_transcript

    def render_message(
        self,
        role: str,
        text: str,
        width: int,
        separated: bool,
        active: bool,
    ) -> str:
        buffer = StringIO()
        console = Console(
            file=buffer,
            force_terminal=True,
            color_system="truecolor",
            no_color=False,
            width=width,
            soft_wrap=False,
            theme=MARKDOWN_THEME,
        )
        if separated and role == "user":
            console.print(Rule(style="#606060"))
        if role == "user":
            console.print(
                Padding(
                    PromptMarkdown(text),
                    (1, 2),
                    style="#f3f4f6 on #4b4b4b",
                    expand=True,
                )
            )
        elif role == "assistant":
            console.print(Text("• ", style="bold #86efac"), end="")
            if text:
                console.print(ChatMarkdown(text, code_theme="monokai"))
            elif active:
                console.print(Text("Thinking…", style="dim italic"))
        else:
            console.print(Text("• ", style="bold #fbbf24"), end="")
            console.print(ChatMarkdown(text, code_theme="monokai"))
        console.print()
        return buffer.getvalue()

    def transcript_cursor_position(self) -> Point:
        if self.follow_output:
            return Point(x=0, y=max(0, self.transcript_line_count - 1))
        return Point(x=0, y=max(0, self.transcript_window.vertical_scroll))

    def render_resume_list(self) -> FormattedText:
        if self.resume_loading:
            return FormattedText([("class:resume.time", " Loading conversations…")])
        if not self.resume_filtered:
            return FormattedText([("class:resume.time", " No matching conversations")])
        fragments: list[tuple[str, str]] = []
        for index, item in enumerate(self.resume_filtered):
            title = item.get("title") or "Untitled"
            updated = item.get("update_time")
            stamp = ""
            if isinstance(updated, (int, float)):
                try:
                    stamp = datetime.fromtimestamp(updated).strftime("%Y-%m-%d %H:%M")
                except (OSError, OverflowError, ValueError):
                    pass
            marker = "›" if index == self.resume_index else " "
            style = "class:resume.selected" if index == self.resume_index else "class:resume.item"
            fragments.append((style, f" {marker} {str(title)[:72]:<72} "))
            fragments.append(("class:resume.time", f"{stamp}\n"))
        return FormattedText(fragments)

    def make_bindings(self) -> KeyBindings:
        bindings = KeyBindings()
        completion_visible = Condition(
            lambda: self.app.layout.current_control is self.input.control
            and self.input.buffer.complete_state is not None
        )

        @bindings.add("enter")
        def submit(event: Any) -> None:
            if event.app.layout.current_control is self.input.control:
                state = self.input.buffer.complete_state
                if state is not None and state.completions:
                    completion = state.current_completion or state.completions[0]
                    self.input.buffer.apply_completion(completion)
                else:
                    self.submit_prompt()

        @bindings.add("tab")
        def complete_command(event: Any) -> None:
            if event.app.layout.current_control is not self.input.control:
                return
            state = self.input.buffer.complete_state
            if state is not None and state.completions:
                completion = state.current_completion or state.completions[0]
                self.input.buffer.apply_completion(completion)
            else:
                self.input.buffer.start_completion(select_first=True)

        @bindings.add("escape", filter=completion_visible, eager=True)
        def dismiss_completion(_event: Any) -> None:
            self.input.buffer.cancel_completion()

        @bindings.add("escape", "enter")
        def newline(event: Any) -> None:
            if event.app.layout.current_control is self.input.control and not self.busy:
                self.input.buffer.insert_text("\n")

        @bindings.add("c-c")
        def interrupt(event: Any) -> None:
            if self.busy:
                self.status = "Stopping response…"
                connection = self.cancel_connection
                if connection is not None:
                    try:
                        connection.shutdown(2)
                    except OSError:
                        pass
                    try:
                        connection.close()
                    except OSError:
                        pass
                self.app.invalidate()
            elif self.input.text:
                self.input.buffer.reset()
                self.status = "Prompt cleared"
                self.app.invalidate()
            else:
                self.status = "Press /exit or Ctrl+D to quit"
                self.app.invalidate()

        @bindings.add("c-d")
        def exit_app(event: Any) -> None:
            if not self.busy and not self.input.text:
                event.app.exit()

        @bindings.add("c-r", eager=True)
        def resume_picker(_event: Any) -> None:
            self.open_resume()

        @bindings.add("c-v", eager=True)
        def paste_image(_event: Any) -> None:
            self.paste_clipboard_image()

        @bindings.add("up", filter=self.resume_filter, eager=True)
        def resume_up(_event: Any) -> None:
            if self.resume_filtered:
                self.resume_index = max(0, self.resume_index - 1)
                self.resume_list_window.vertical_scroll = max(0, self.resume_index - 2)
                self.app.invalidate()

        @bindings.add("down", filter=self.resume_filter, eager=True)
        def resume_down(_event: Any) -> None:
            if self.resume_filtered:
                self.resume_index = min(
                    len(self.resume_filtered) - 1, self.resume_index + 1
                )
                self.resume_list_window.vertical_scroll = max(0, self.resume_index - 2)
                self.app.invalidate()

        @bindings.add("enter", filter=self.resume_filter, eager=True)
        def resume_selected(_event: Any) -> None:
            self.select_resume()

        @bindings.add("escape", filter=self.resume_filter, eager=True)
        def close_resume(_event: Any) -> None:
            self.close_resume()

        @bindings.add("pageup")
        def page_up(_event: Any) -> None:
            self.pause_following()
            page = self.transcript_page_size()
            self.transcript_window.vertical_scroll = max(
                0, self.transcript_window.vertical_scroll - page
            )

        @bindings.add("pagedown")
        def page_down(_event: Any) -> None:
            self.pause_following()
            self.transcript_window.vertical_scroll += self.transcript_page_size()

        @bindings.add("home", eager=True)
        def prompt_line_start(event: Any) -> None:
            if event.app.layout.current_control is self.input.control:
                self.input.buffer.cursor_position += (
                    self.input.buffer.document.get_start_of_line_position()
                )

        @bindings.add("end", eager=True)
        def prompt_line_end(event: Any) -> None:
            if event.app.layout.current_control is self.input.control:
                self.input.buffer.cursor_position += (
                    self.input.buffer.document.get_end_of_line_position()
                )
                return
            self.follow_output = True
            self.transcript_window.vertical_scroll = 10**9
            self.app.invalidate()

        return bindings

    def pause_following(self) -> None:
        self.follow_output = False

    def transcript_page_size(self) -> int:
        info = self.transcript_window.render_info
        if info is None:
            return 10
        return max(3, info.window_height - 3)

    def append_system(self, text: str) -> None:
        with self.lock:
            self.messages.append({"role": "system", "text": text})
        self.scroll_bottom()

    def scroll_bottom(self, force: bool = True) -> None:
        if force:
            self.follow_output = True
        if self.follow_output:
            self.transcript_window.vertical_scroll = 10**9
        self.app.invalidate()

    def submit_prompt(self) -> None:
        if self.loading_conversation:
            self.status = "Conversation still loading · draft kept"
            self.app.invalidate()
            return
        if self.busy:
            self.status = "Response active · draft kept for the next turn"
            self.app.invalidate()
            return
        prompt = self.input.text.strip()
        attachments = list(self.pending_attachments)
        if not prompt and not attachments:
            return
        self.input.text = ""
        if prompt.startswith("/"):
            self.handle_command(prompt)
            return
        self.pending_attachments.clear()
        prompt = prompt or "Describe the attached image."
        display_prompt = prompt
        if attachments:
            labels = " ".join(f"[📎 {item['name']}]" for item in attachments)
            display_prompt = f"{display_prompt}\n\n{labels}"
        with self.lock:
            self.messages.append({"role": "user", "text": display_prompt})
            self.messages.append({"role": "assistant", "text": ""})
        self.busy = True
        self.active_started = time.monotonic()
        self.last_elapsed = None
        self.status = "Preparing secure request…"
        self.scroll_bottom()
        threading.Thread(
            target=self.run_send, args=(prompt, attachments), daemon=True
        ).start()

    def handle_command(self, command: str) -> None:
        name, _, argument = command.partition(" ")
        if name in ("/exit", "/quit"):
            self.app.exit()
        elif name == "/new":
            with self.lock:
                self.messages.clear()
                self.committed_message_count = 0
                self.history_tail = ""
            self.conversation_id = None
            self.title = "New conversation"
            self.pending_attachments.clear()
            self.status = "Started a new conversation"
            self.scroll_bottom()
        elif name == "/clear":
            with self.lock:
                self.messages.clear()
                self.committed_message_count = 0
                self.history_tail = ""
            self.status = "Transcript cleared"
            self.scroll_bottom()
        elif name == "/resume":
            if argument.strip():
                self.resume_conversation(argument.strip())
            else:
                self.open_resume()
        elif name == "/history":
            if self.conversation_id:
                self.resume_conversation(self.conversation_id)
            else:
                self.status = "No saved conversation yet"
                self.app.invalidate()
        elif name == "/rename":
            self.rename_current_conversation(argument)
        elif name == "/copy":
            self.copy_latest_response()
        elif name == "/remove":
            if self.pending_attachments:
                removed = self.pending_attachments.pop()
                self.status = f"Removed {removed['name']}"
            else:
                self.status = "No pending image to remove"
            self.app.invalidate()
        elif name == "/help":
            self.append_system(
                "**Commands**\n\n"
                "- `/new` start a conversation\n"
                "- `/resume` search recent conversations\n"
                "- `/resume ID` open a conversation directly\n"
                "- `/history` reload this conversation\n"
                "- `/rename TITLE` rename this conversation\n"
                "- `/copy` copy the latest assistant response\n"
                "- `/remove` remove the latest pending image\n"
                "- `/clear` clear the displayed transcript\n"
                "- `/exit` or `/quit` quit"
            )
        else:
            self.status = f"Unknown command: {name}"
            self.app.invalidate()

    def rename_current_conversation(self, title: str) -> None:
        title = title.strip()
        if self.conversation_id is None:
            self.status = "Save the conversation before renaming it"
            self.app.invalidate()
            return
        if not title:
            self.status = "Usage: /rename TITLE"
            self.app.invalidate()
            return
        if self.rename_conversation is None:
            self.status = "Conversation rename is unavailable"
            self.app.invalidate()
            return
        conversation_id = self.conversation_id
        self.status = "Renaming conversation…"
        self.app.invalidate()

        def rename() -> None:
            try:
                self.rename_conversation(conversation_id, title)
                self.title = title
                self.status = "Conversation renamed"
            except Exception as error:
                self.status = f"Rename failed: {error}"
            self.app.invalidate()

        threading.Thread(target=rename, daemon=True).start()

    @staticmethod
    def read_clipboard_png() -> bytes:
        commands = []
        if shutil.which("wl-paste"):
            commands.append(["wl-paste", "--no-newline", "--type", "image/png"])
        if shutil.which("xclip"):
            commands.append(["xclip", "-selection", "clipboard", "-t", "image/png", "-o"])
        if shutil.which("pngpaste"):
            commands.append(["pngpaste", "-"])
        for command in commands:
            try:
                result = subprocess.run(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    timeout=5,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError):
                continue
            if result.returncode == 0 and result.stdout.startswith(b"\x89PNG\r\n\x1a\n"):
                return result.stdout
        raise RuntimeError("clipboard does not contain a PNG image")

    def paste_clipboard_image(self) -> None:
        if self.uploading_image:
            self.status = "An image upload is already in progress"
            self.app.invalidate()
            return
        if self.upload_image is None:
            self.status = "Image upload is unavailable"
            self.app.invalidate()
            return
        self.uploading_image = True
        self.upload_started = time.monotonic()
        self.status = "Reading image from clipboard…"
        self.app.invalidate()

        def upload() -> None:
            try:
                data = self.read_clipboard_png()
                if len(data) < 24:
                    raise RuntimeError("clipboard PNG is truncated")
                width = int.from_bytes(data[16:20], "big")
                height = int.from_bytes(data[20:24], "big")
                if width < 1 or height < 1:
                    raise RuntimeError("clipboard PNG has invalid dimensions")
                name = datetime.now().strftime("clipboard-%Y%m%d-%H%M%S.png")
                self.status = f"Uploading {width}×{height} image…"
                self.app.invalidate()
                attachment = self.upload_image(
                    data, name, "image/png", width, height
                )
                if not isinstance(attachment, dict) or not attachment.get("id"):
                    raise RuntimeError("image upload returned no file ID")
                self.pending_attachments.append(attachment)
                self.status = "Ready"
            except Exception as error:
                self.status = f"Image paste failed: {error}"
            finally:
                self.uploading_image = False
                self.upload_started = None
                self.app.invalidate()

        threading.Thread(target=upload, daemon=True).start()

    def copy_latest_response(self) -> None:
        with self.lock:
            response = next(
                (
                    message["text"]
                    for message in reversed(self.messages)
                    if message["role"] == "assistant" and message["text"]
                ),
                "",
            )
        if not response:
            self.status = "No assistant response to copy"
            self.app.invalidate()
            return

        response = render_chatgpt_annotations(response)
        clipboard_commands = (
            ("wl-copy", ["wl-copy"]),
            ("xclip", ["xclip", "-selection", "clipboard"]),
            ("xsel", ["xsel", "--clipboard", "--input"]),
            ("pbcopy", ["pbcopy"]),
        )
        for executable, command in clipboard_commands:
            if shutil.which(executable) is None:
                continue
            try:
                subprocess.run(
                    command,
                    input=response,
                    text=True,
                    check=True,
                    timeout=3,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                self.status = "Copied latest response"
                self.app.invalidate()
                return
            except (OSError, subprocess.SubprocessError):
                continue

        # OSC 52 is supported by many terminal emulators and remote sessions.
        encoded = base64.b64encode(response.encode()).decode("ascii")
        self.app.output.write_raw(f"\x1b]52;c;{encoded}\x07")
        self.app.output.flush()
        self.status = "Sent latest response to the terminal clipboard"
        self.app.invalidate()

    def open_resume(self) -> None:
        if self.busy or self.loading_conversation or self.resume_visible:
            return
        self.resume_visible = True
        self.resume_loading = True
        self.resume_items = []
        self.resume_filtered = []
        self.resume_index = 0
        self.resume_search.text = ""
        self.status = "Loading recent conversations…"
        self.app.layout.focus(self.resume_search)
        self.app.invalidate()

        def load_items() -> None:
            try:
                items = self.list_conversations()
                self.resume_items = items
                self.resume_loading = False
                self.filter_resume()
                self.status = f"{len(items)} recent conversations"
            except Exception as error:
                self.resume_loading = False
                self.status = f"Conversation list failed: {error}"
            self.app.invalidate()

        threading.Thread(target=load_items, daemon=True).start()

    def filter_resume(self) -> None:
        query = self.resume_search.text.strip().casefold()
        self.resume_filtered = [
            item for item in self.resume_items
            if query in str(item.get("title") or "Untitled").casefold()
            or query in str(item.get("id") or "").casefold()
        ]
        self.resume_index = min(
            self.resume_index, max(0, len(self.resume_filtered) - 1)
        )
        self.resume_list_window.vertical_scroll = max(0, self.resume_index - 2)
        if hasattr(self, "app"):
            self.app.invalidate()

    def close_resume(self) -> None:
        self.resume_visible = False
        self.status = "Ready"
        self.app.layout.focus(self.input)
        self.app.invalidate()

    def select_resume(self) -> None:
        if not self.resume_filtered:
            return
        item = self.resume_filtered[self.resume_index]
        conversation_id = item.get("id")
        if not isinstance(conversation_id, str) or not conversation_id:
            return
        self.close_resume()
        self.resume_conversation(conversation_id)

    def resume_conversation(self, conversation_id: str) -> None:
        self.loading_conversation = True
        self.loading_started = time.monotonic()
        self.status = "Loading conversation…"
        self.app.invalidate()

        def load_selected() -> None:
            try:
                conversation = self.load_conversation(conversation_id)
                messages = []
                for message in conversation.get("messages", []):
                    visible = visible_chat_message(message)
                    if visible is not None:
                        messages.append(visible)
                with self.lock:
                    self.messages = messages
                self.conversation_id = conversation_id
                self.title = conversation.get("title") or "Untitled"
                self.commit_loaded_history(messages)
            except Exception as error:
                self.loading_conversation = False
                self.loading_started = None
                self.status = f"Load failed: {error}"
                self.app.invalidate()

        threading.Thread(target=load_selected, daemon=True).start()

    def run_send(self, prompt: str, attachments: list[dict[str, Any]]) -> None:
        rendered = ""

        def register_connection(connection: Any) -> None:
            self.cancel_connection = connection

        def on_event(event: dict[str, Any]) -> None:
            nonlocal rendered
            event_type = event.get("type")
            text = event.get("text")
            if event_type == "delta" and isinstance(text, str):
                rendered += text
            elif event_type == "replace" and isinstance(text, str):
                rendered = text
            else:
                return
            with self.lock:
                self.messages[-1]["text"] = rendered
            self.status = "Streaming response…"
            self.scroll_bottom(force=False)

        try:
            result = self.send_prompt(
                self.conversation_id,
                prompt,
                attachments,
                on_event,
                register_connection,
            )
            self.conversation_id = result.get("conversation_id", self.conversation_id)
            conversation = result.get("conversation")
            if isinstance(conversation, dict):
                self.title = conversation.get("title") or self.title
                response_model = conversation.get("default_model_slug")
                if isinstance(response_model, str) and response_model:
                    self.model = response_model
                previous_ids = set(result.get("previous_ids", []))
                final_text = ""
                for message in conversation.get("messages", []):
                    if not isinstance(message, dict) or message.get("id") in previous_ids:
                        continue
                    author = message.get("author") or {}
                    if author.get("role") != "assistant":
                        continue
                    content = message.get("content") or {}
                    parts = content.get("parts") or []
                    text_parts = [part for part in parts if isinstance(part, str)]
                    if text_parts:
                        final_text = "\n".join(text_parts)
                if final_text and final_text != rendered:
                    with self.lock:
                        self.messages[-1]["text"] = final_text
            self.status = "Ready"
        except Exception as error:
            self.pending_attachments[0:0] = attachments
            if not rendered:
                with self.lock:
                    self.messages[-1]["text"] = f"*Request stopped: {error}*"
            self.status = "Response stopped"
        finally:
            if self.active_started is not None:
                self.last_elapsed = time.monotonic() - self.active_started
            self.active_started = None
            self.cancel_connection = None
            self.busy = False
            self.commit_completed_turns()

    def run(self) -> None:
        self.reset_terminal_session()
        self.commit_initial_history()

        def started() -> None:
            self.event_loop = asyncio.get_running_loop()
            if self.load_on_start and self.conversation_id is not None:
                self.resume_conversation(self.conversation_id)

        self.app.run(pre_run=started)


def run_tui(
    list_conversations: ListConversations,
    load_conversation: LoadConversation,
    send_prompt: SendPrompt,
    conversation_id: str | None = None,
    title: str = "New conversation",
    initial_messages: list[dict[str, Any]] | None = None,
    model: str | None = None,
    upload_image: UploadImage | None = None,
    rename_conversation: RenameConversation | None = None,
) -> None:
    ChatTui(
        list_conversations,
        load_conversation,
        send_prompt,
        conversation_id,
        title,
        initial_messages,
        model,
        upload_image,
        rename_conversation,
    ).run()
