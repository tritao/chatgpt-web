"""Fullscreen terminal interface for chatgpt-web."""

from __future__ import annotations

from io import StringIO
import threading
from typing import Any, Callable

from prompt_toolkit.application import Application
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import ANSI, FormattedText
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import D
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import Frame, TextArea
from rich.console import Console
from rich.markdown import Markdown
from rich.text import Text


Message = dict[str, str]
LoadConversation = Callable[[str], dict[str, Any]]
SendPrompt = Callable[
    [str | None, str, Callable[[dict[str, Any]], None], Callable[[Any], None]],
    dict[str, Any],
]


class ChatTui:
    def __init__(
        self,
        load_conversation: LoadConversation,
        send_prompt: SendPrompt,
        conversation_id: str | None = None,
        title: str = "New conversation",
        initial_messages: list[dict[str, Any]] | None = None,
    ) -> None:
        self.load_conversation = load_conversation
        self.send_prompt = send_prompt
        self.conversation_id = conversation_id
        self.title = title
        self.messages: list[Message] = []
        for message in initial_messages or []:
            role = message.get("role")
            text = message.get("text")
            if isinstance(role, str) and isinstance(text, str) and text:
                self.messages.append({"role": role, "text": text})
        self.busy = False
        self.status = "Ready"
        self.cancel_connection: Any = None
        self.lock = threading.RLock()

        self.transcript_control = FormattedTextControl(
            text=lambda: ANSI(self.render_transcript()),
            focusable=True,
        )
        self.transcript_window = Window(
            content=self.transcript_control,
            wrap_lines=True,
            right_margins=[],
            allow_scroll_beyond_bottom=False,
        )
        self.input = TextArea(
            height=D(min=3, max=8),
            multiline=True,
            wrap_lines=True,
            prompt="❯ ",
            read_only=Condition(lambda: self.busy),
        )
        self.bindings = self.make_bindings()
        root = HSplit([
            Window(
                height=1,
                content=FormattedTextControl(self.render_header),
                style="class:header",
            ),
            Window(height=1, char="─", style="class:separator"),
            self.transcript_window,
            Window(height=1, char="─", style="class:separator"),
            Window(
                height=1,
                content=FormattedTextControl(self.render_status),
                style="class:status",
            ),
            Frame(self.input, title="Prompt"),
            Window(
                height=1,
                content=FormattedTextControl(
                    text=" Enter send  Alt+Enter newline  Ctrl+C stop  /help commands "
                ),
                style="class:help",
            ),
        ])
        self.app: Application[Any] = Application(
            layout=Layout(root, focused_element=self.input),
            key_bindings=self.bindings,
            full_screen=True,
            mouse_support=True,
            style=Style.from_dict({
                "header": "bg:#1f2937 #f9fafb",
                "brand": "bold #5eead4",
                "mode": "#9ca3af",
                "separator": "#4b5563",
                "status": "bg:#111827 #d1d5db",
                "help": "bg:#1f2937 #9ca3af",
                "frame.label": "#5eead4",
                "frame.border": "#4b5563",
            }),
        )

    def render_header(self) -> FormattedText:
        mode = "generating" if self.busy else "ready"
        return FormattedText([
            ("class:brand", " ChatGPT Web "),
            ("", f"· {self.title} "),
            ("class:mode", f"[{mode}]"),
        ])

    def render_status(self) -> FormattedText:
        return FormattedText([("", f" {self.status}")])

    def render_transcript(self) -> str:
        with self.lock:
            messages = [dict(message) for message in self.messages]
        width = 100
        if hasattr(self, "app"):
            try:
                width = max(40, self.app.output.get_size().columns - 3)
            except Exception:
                pass
        buffer = StringIO()
        console = Console(
            file=buffer,
            force_terminal=True,
            color_system="truecolor",
            width=width,
            soft_wrap=False,
        )
        if not messages:
            console.print(Text("Start a conversation below.", style="dim"))
        for index, message in enumerate(messages):
            if index:
                console.print()
            role = message["role"]
            text = message["text"]
            if role == "user":
                console.print(Text("You", style="bold cyan"))
                console.print(Text(text, style="white"))
            elif role == "assistant":
                console.print(Text("ChatGPT", style="bold green"))
                if text:
                    console.print(Markdown(text, code_theme="monokai"))
                elif self.busy:
                    console.print(Text("Thinking…", style="dim italic"))
            else:
                console.print(Text(role.title(), style="bold yellow"))
                console.print(Markdown(text))
        return buffer.getvalue()

    def make_bindings(self) -> KeyBindings:
        bindings = KeyBindings()

        @bindings.add("enter")
        def submit(event: Any) -> None:
            if event.app.layout.current_control is self.input.control:
                self.submit_prompt()

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
            else:
                self.status = "Press /exit or Ctrl+D to quit"
                self.app.invalidate()

        @bindings.add("c-d")
        def exit_app(event: Any) -> None:
            if not self.busy and not self.input.text:
                event.app.exit()

        @bindings.add("pageup")
        def page_up(_event: Any) -> None:
            self.transcript_window.vertical_scroll = max(
                0, self.transcript_window.vertical_scroll - 10
            )

        @bindings.add("pagedown")
        def page_down(_event: Any) -> None:
            self.transcript_window.vertical_scroll += 10

        return bindings

    def append_system(self, text: str) -> None:
        with self.lock:
            self.messages.append({"role": "system", "text": text})
        self.scroll_bottom()

    def scroll_bottom(self) -> None:
        self.transcript_window.vertical_scroll = 10**9
        self.app.invalidate()

    def submit_prompt(self) -> None:
        if self.busy:
            return
        prompt = self.input.text.strip()
        if not prompt:
            return
        self.input.text = ""
        if prompt.startswith("/"):
            self.handle_command(prompt)
            return
        with self.lock:
            self.messages.append({"role": "user", "text": prompt})
            self.messages.append({"role": "assistant", "text": ""})
        self.busy = True
        self.status = "Preparing secure request…"
        self.scroll_bottom()
        threading.Thread(target=self.run_send, args=(prompt,), daemon=True).start()

    def handle_command(self, command: str) -> None:
        name, _, argument = command.partition(" ")
        if name in ("/exit", "/quit"):
            self.app.exit()
        elif name == "/new":
            with self.lock:
                self.messages.clear()
            self.conversation_id = None
            self.title = "New conversation"
            self.status = "Started a new conversation"
            self.scroll_bottom()
        elif name == "/clear":
            with self.lock:
                self.messages.clear()
            self.status = "Transcript cleared"
            self.scroll_bottom()
        elif name == "/resume" and argument.strip():
            self.resume_conversation(argument.strip())
        elif name == "/history":
            if self.conversation_id:
                self.resume_conversation(self.conversation_id)
            else:
                self.status = "No saved conversation yet"
                self.app.invalidate()
        elif name == "/help":
            self.append_system(
                "**Commands**\n\n"
                "- `/new` start a conversation\n"
                "- `/resume ID` open a conversation\n"
                "- `/history` reload this conversation\n"
                "- `/clear` clear the displayed transcript\n"
                "- `/exit` quit"
            )
        else:
            self.status = f"Unknown command: {name}"
            self.app.invalidate()

    def resume_conversation(self, conversation_id: str) -> None:
        self.status = "Loading conversation…"
        self.app.invalidate()
        try:
            conversation = self.load_conversation(conversation_id)
            messages = []
            for message in conversation.get("messages", []):
                role, text = message.get("role"), message.get("text")
                if isinstance(role, str) and isinstance(text, str) and text:
                    messages.append({"role": role, "text": text})
            with self.lock:
                self.messages = messages
            self.conversation_id = conversation_id
            self.title = conversation.get("title") or "Untitled"
            self.status = "Ready"
            self.scroll_bottom()
        except Exception as error:
            self.status = f"Load failed: {error}"
            self.app.invalidate()

    def run_send(self, prompt: str) -> None:
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
            self.scroll_bottom()

        try:
            result = self.send_prompt(
                self.conversation_id, prompt, on_event, register_connection
            )
            self.conversation_id = result.get("conversation_id", self.conversation_id)
            conversation = result.get("conversation")
            if isinstance(conversation, dict):
                self.title = conversation.get("title") or self.title
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
            if not rendered:
                with self.lock:
                    self.messages[-1]["text"] = f"*Request stopped: {error}*"
            self.status = "Response stopped"
        finally:
            self.cancel_connection = None
            self.busy = False
            self.scroll_bottom()

    def run(self) -> None:
        self.app.run()


def run_tui(
    load_conversation: LoadConversation,
    send_prompt: SendPrompt,
    conversation_id: str | None = None,
    title: str = "New conversation",
    initial_messages: list[dict[str, Any]] | None = None,
) -> None:
    ChatTui(
        load_conversation,
        send_prompt,
        conversation_id,
        title,
        initial_messages,
    ).run()
