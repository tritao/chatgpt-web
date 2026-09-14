"""Fullscreen terminal interface for chatgpt-web."""

from __future__ import annotations

from io import StringIO
import re
import threading
from datetime import datetime
from typing import Any, Callable

from prompt_toolkit.application import Application
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import ANSI, FormattedText
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Float, FloatContainer, HSplit, Layout, Window
from prompt_toolkit.layout.containers import ConditionalContainer
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import D
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import Frame, TextArea
from rich.console import Console
from rich.markdown import Markdown
from rich.text import Text


Message = dict[str, str]
LoadConversation = Callable[[str], dict[str, Any]]
ListConversations = Callable[[], list[dict[str, Any]]]
SendPrompt = Callable[
    [str | None, str, Callable[[dict[str, Any]], None], Callable[[Any], None]],
    dict[str, Any],
]

ANNOTATION_PATTERN = re.compile(r"\ue200([^\ue201\ue202]+)(?:\ue202(.*?))?\ue201")


def render_chatgpt_annotations(text: str) -> str:
    """Turn ChatGPT web's private-use annotations into terminal-safe text."""

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
    # Streaming can end between the opening marker and its terminator. Hide the
    # incomplete suffix until the next delta completes it.
    rendered = re.sub(r"\ue200[^\ue201]*$", "", rendered)
    rendered = rendered.replace("\ue200", "").replace("\ue201", "").replace("\ue202", "")
    rendered = re.sub(r"[ \t]+([,.;:!?])", r"\1", rendered)
    return rendered


class ChatTui:
    def __init__(
        self,
        list_conversations: ListConversations,
        load_conversation: LoadConversation,
        send_prompt: SendPrompt,
        conversation_id: str | None = None,
        title: str = "New conversation",
        initial_messages: list[dict[str, Any]] | None = None,
    ) -> None:
        self.list_conversations = list_conversations
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
        self.resume_visible = False
        self.resume_loading = False
        self.resume_items: list[dict[str, Any]] = []
        self.resume_filtered: list[dict[str, Any]] = []
        self.resume_index = 0
        self.resume_filter = Condition(lambda: self.resume_visible)

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
        base = HSplit([
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
                    text=" Enter send  Alt+Enter newline  Ctrl+R resume  Ctrl+C stop "
                ),
                style="class:help",
            ),
        ])
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
            floats=[Float(
                content=resume_dialog,
                left=4,
                right=4,
                top=2,
                bottom=2,
            )],
        )
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
                "resume.selected": "bg:#0f766e #ffffff bold",
                "resume.item": "#d1d5db",
                "resume.time": "#9ca3af",
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
            text = render_chatgpt_annotations(message["text"])
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

        @bindings.add("c-r", eager=True)
        def resume_picker(_event: Any) -> None:
            self.open_resume()

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
        elif name == "/help":
            self.append_system(
                "**Commands**\n\n"
                "- `/new` start a conversation\n"
                "- `/resume` search recent conversations\n"
                "- `/resume ID` open a conversation directly\n"
                "- `/history` reload this conversation\n"
                "- `/clear` clear the displayed transcript\n"
                "- `/exit` quit"
            )
        else:
            self.status = f"Unknown command: {name}"
            self.app.invalidate()

    def open_resume(self) -> None:
        if self.busy or self.resume_visible:
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
        self.status = "Loading conversation…"
        self.app.invalidate()

        def load_selected() -> None:
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

        threading.Thread(target=load_selected, daemon=True).start()

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
    list_conversations: ListConversations,
    load_conversation: LoadConversation,
    send_prompt: SendPrompt,
    conversation_id: str | None = None,
    title: str = "New conversation",
    initial_messages: list[dict[str, Any]] | None = None,
) -> None:
    ChatTui(
        list_conversations,
        load_conversation,
        send_prompt,
        conversation_id,
        title,
        initial_messages,
    ).run()
