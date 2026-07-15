"""Full-screen Textual TUI — the UI that makes the agent visible.

Modeled on gemma's `ui/tui.py` (Agent = Model + Harness + UI): two panes —
conversation and activity — an input pinned at the bottom, and the approval
gate as a MODAL instead of an inline y/N prompt. The modal is the fix for
the line-REPL's core weakness: a confirmation that arrives while the prompt
is pending can't fight for the same terminal line; here it takes the screen,
fail-closed, until the user answers.

Where gemma bridges a *synchronous* agent to Textual with worker threads +
threading.Event, our agent is async — a turn is just a task on the same
loop, and the approval bridge is a plain awaited future.

Seams into the existing harness (no agent changes needed):
  - `_confirm.set_confirmer(...)`  — y/N gates (shell_exec / write_file /
    edit_file / plan_and_confirm) become the ApprovalModal.
  - `set_tui_session(adapter)`     — human_feedback / background workflow
    pauses route their question into the conversation pane and read the
    next input as the reply.
  - `App.begin_capture_print`      — stray print()/stderr output from tools
    lands in the activity pane instead of corrupting the screen.

Textual is an optional extra: `pip install 'botcircuits-agent[tui]'`.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from botcircuits.agent import Agent
    from botcircuits.providers.base import LLMProvider
    from botcircuits.cli.commands import CLIState


def run_tui_available() -> str | None:
    """None when textual is importable, else the install hint to show."""
    try:
        import textual  # noqa: F401
    except ImportError:
        return ("the Textual TUI needs the optional 'tui' extra:\n"
                "    pip install 'botcircuits-agent[tui]'   (or: uv add textual)")
    return None


async def run_tui(agent: "Agent", provider: "LLMProvider",
                  state: "CLIState") -> int:
    """Build and run the Textual app on the current event loop."""
    app = _build_app(agent, provider, state)
    await app.run_async()
    return 0


def _build_app(agent, provider, state):
    """All textual imports live inside this factory so the module imports
    cleanly (for the REPL / tests) without the extra installed."""
    from rich.markdown import Markdown
    from rich.text import Text
    from textual import events
    from textual.app import App, ComposeResult
    from textual.containers import Horizontal, Vertical, VerticalScroll
    from textual.screen import ModalScreen
    from textual.widgets import Button, Footer, Input, Label, RichLog, Static

    from botcircuits.agent.tools.builtins import _confirm
    from botcircuits.cli.render import preview, _tool_icon
    from botcircuits.cli.tui import set_tui_session

    class ApprovalModal(ModalScreen[bool]):
        """The approval gate as a modal. Pauses the turn until the user
        answers; fail-closed (escape / d = deny)."""

        BINDINGS = [("y", "allow", "allow"), ("a", "allow", "allow"),
                    ("n", "deny", "deny"), ("d", "deny", "deny"),
                    ("escape", "deny", "deny")]

        def __init__(self, title: str, lines: list[str]) -> None:
            super().__init__()
            self._title = title
            self._lines = lines

        def compose(self) -> ComposeResult:
            with Vertical(id="approval"):
                yield Static(f"approval required · {self._title}",
                             id="approval-title")
                yield Static("\n".join(self._lines), id="approval-body")
                yield Static("runs fail-closed · y allow · n deny",
                             id="approval-note")
                with Horizontal(id="approval-actions"):
                    yield Button("allow · y", id="allow", variant="success")
                    yield Button("deny · n", id="deny", variant="error")

        def on_button_pressed(self, event: Button.Pressed) -> None:
            self.dismiss(event.button.id == "allow")

        def action_allow(self) -> None:
            self.dismiss(True)

        def action_deny(self) -> None:
            self.dismiss(False)

    class _PauseAdapter:
        """Quacks enough like `TUISession` for the existing pause consumers
        (human_feedback, WorkflowTask.pause, Spinner's is-a-TUI check)."""

        def __init__(self, app: "BotCircuitsTUI") -> None:
            self._app = app

        async def pause(self, question: str) -> str:
            return await self._app.ask_user(question)

        def submit(self, coro) -> asyncio.Task:
            return self._app.submit_background(coro)

        def is_paused(self) -> bool:
            return self._app._pending_reply is not None

    class BotCircuitsTUI(App):
        """Two panes (conversation · activity) + header + input + footer."""

        CSS = """
        #header { height: 1; padding: 0 1; background: $panel; color: $accent; }
        #body { height: 1fr; }
        .pane { height: 1fr; }
        #conversation { width: 1fr; border-right: solid $panel; }
        #activity-pane { width: 44; }
        #log { height: 1fr; }
        #activity { height: 1fr; padding: 0 1; }
        #prompt { border: none; border-top: solid $panel; height: 3;
                  padding: 0 1; dock: bottom; }
        #activity-foot { height: 1; dock: bottom; padding: 0 1;
                         color: $text-muted; border-top: solid $panel; }
        .msg { height: auto; padding: 0 1; margin: 0 1 1 1; }
        .msg-role { text-style: bold; }
        .msg-user { background: $surface-lighten-1; }
        .msg-agent { background: $panel; }
        .msg-agent .msg-role { color: $accent; }
        .msg-question { border-left: thick $warning; }
        ApprovalModal { align: center middle; }
        #approval { width: 76; height: auto; max-height: 80%;
                    border: thick $warning; padding: 1 2; }
        #approval-title { color: $warning; text-style: bold; }
        #approval-note { color: $text-muted; margin-top: 1; }
        #approval-actions { height: auto; margin-top: 1; }
        #approval-actions Button { margin-right: 2; }
        """

        BINDINGS = [("ctrl+n", "new_session", "new"),
                    ("ctrl+q", "quit", "quit")]

        def __init__(self) -> None:
            super().__init__()
            self.agent = agent
            self.provider = provider
            self.state = state
            self._busy = False
            # A tool / workflow is waiting for the user's next input line.
            self._pending_reply: Optional[asyncio.Future] = None
            # Live streaming block for the current turn.
            self._live_body: Optional[Static] = None
            self._live_text = ""
            self._bg_tasks: list[asyncio.Task] = []

        # -- layout ----------------------------------------------------------

        def compose(self) -> ComposeResult:
            yield Static(id="header")
            with Horizontal(id="body"):
                with Vertical(id="conversation", classes="pane"):
                    yield VerticalScroll(id="log")
                    yield Input(
                        placeholder="type a message…  ·  /plan <task>  /reset  /new",
                        id="prompt",
                    )
                with Vertical(id="activity-pane", classes="pane"):
                    yield RichLog(id="activity", wrap=True, markup=False)
                    yield Static("0 tokens", id="activity-foot")
            yield Footer()

        def on_mount(self) -> None:
            self._refresh_header()
            # Route the harness's interaction seams into this app.
            _confirm.set_confirmer(self._confirm_modal)
            set_tui_session(_PauseAdapter(self))
            # Stray print()/stderr from tools → activity pane, not the screen.
            self.begin_capture_print(self)
            self.query_one("#prompt", Input).focus()

        def on_unmount(self) -> None:
            _confirm.set_confirmer(None)
            set_tui_session(None)
            for t in self._bg_tasks:
                if not t.done():
                    t.cancel()

        def on_print(self, event: events.Print) -> None:
            text = event.text.rstrip()
            if text:
                self._activity(Text(text, style="dim"))

        # -- rendering helpers -------------------------------------------------

        def _refresh_header(self) -> None:
            sid = self.state.session_id or "(new)"
            self.query_one("#header", Static).update(
                f"⚙ {self.provider.name}:{self.provider.model}   ·   "
                f"session: {sid}   ·   tools: {len(self.agent.tools.all())}"
            )

        def _activity(self, renderable) -> None:
            self.query_one("#activity", RichLog).write(renderable)

        def _refresh_usage(self) -> None:
            total = (self.provider.usage_input_tokens
                     + self.provider.usage_output_tokens)
            tok = f"{total / 1000:.1f}K" if total >= 1000 else str(total)
            self.query_one("#activity-foot", Static).update(f"{tok} tokens")

        def _mount_block(self, role_label: str, body, role_class: str) -> Static:
            log = self.query_one("#log", VerticalScroll)
            body_widget = Static(body, classes="msg-body")
            block = Vertical(
                Label(role_label, classes="msg-role"),
                body_widget,
                classes=f"msg {role_class}",
            )
            log.mount(block)
            log.scroll_end(animate=False)
            return body_widget

        def _write_user(self, text: str) -> None:
            self._mount_block("you ▸", text, "msg-user")

        def _write_agent(self, text: str) -> None:
            self._mount_block("argus ▸", Markdown(text), "msg-agent")

        def _write_question(self, text: str) -> None:
            self._mount_block("argus ▸", Markdown(text),
                              "msg-agent msg-question")

        # -- streaming ---------------------------------------------------------

        def _stream_delta(self, piece: str) -> None:
            if self._live_body is None:
                self._live_body = self._mount_block("argus ▸", Markdown(""),
                                                    "msg-agent")
                self._live_text = ""
            self._live_text += piece
            self._live_body.update(Markdown(self._live_text))
            self.query_one("#log", VerticalScroll).scroll_end(animate=False)

        def _finalize_stream(self, final_text: str) -> None:
            if self._live_body is not None:
                if final_text and final_text != self._live_text:
                    self._live_body.update(Markdown(final_text))
                self._live_body = None
                self._live_text = ""
            elif final_text:
                self._write_agent(final_text)

        # -- input dispatch ------------------------------------------------------

        def on_input_submitted(self, event: Input.Submitted) -> None:
            text = event.value.strip()
            if not text:
                return
            event.input.value = ""

            # A paused tool/workflow gets the line as its reply.
            if self._pending_reply is not None and not self._pending_reply.done():
                self._write_user(text)
                fut, self._pending_reply = self._pending_reply, None
                fut.set_result(text)
                return

            if text.startswith("/"):
                self._handle_command(text)
                return

            if self._busy:
                self._activity(Text("(turn in progress — wait or Ctrl+Q)",
                                    style="yellow"))
                return

            self._write_user(text)
            self._busy = True
            self.run_worker(self._run_turn(text), exclusive=False)

        # -- the agent turn ------------------------------------------------------

        async def _run_turn(self, text: str) -> None:
            t0 = time.monotonic()
            try:
                async for ev in self.agent.chat_stream(
                        text, session_id=self.state.session_id,
                        system=self.state.system):
                    if ev.session_id and not self.state.session_id:
                        self.state.session_id = ev.session_id
                        self._refresh_header()
                    if ev.type == "text_delta":
                        self._stream_delta(ev.text or "")
                    elif ev.type == "tool_call":
                        tc = ev.tool_call
                        self._activity(Text(
                            f"{_tool_icon(tc.name)} {tc.name}  "
                            f"{preview(str(tc.arguments), 60)}"))
                    elif ev.type == "tool_result":
                        style = "red" if ev.is_error else "green"
                        label = "error" if ev.is_error else "result"
                        self._activity(Text(
                            f"  ◂ {label}  {preview(ev.text or '', 100)}",
                            style=style))
                    elif ev.type == "done":
                        self._finalize_stream(ev.text or "")
                    elif ev.type == "error":
                        self._finalize_stream("")
                        self._write_agent(f"**[error]** {ev.text}")
            except Exception as e:  # surface, don't crash the UI
                self._finalize_stream("")
                self._write_agent(f"**[error]** {type(e).__name__}: {e}")
            finally:
                self._busy = False
                self._activity(Text(f"— turn done in {time.monotonic() - t0:.0f}s",
                                    style="dim"))
                self._refresh_usage()
                self.query_one("#prompt", Input).focus()

        # -- harness seams ---------------------------------------------------------

        async def _confirm_modal(self, title: str, lines: list,
                                 prompt: str) -> bool:
            """The y/N gate as a modal. Safe to call from any task on the
            loop: push_screen is scheduled on the message pump and the
            decision comes back through a plain future (fail-closed)."""
            fut: asyncio.Future = asyncio.get_running_loop().create_future()

            def _done(allowed: bool | None) -> None:
                if not fut.done():
                    fut.set_result(bool(allowed))

            self.call_later(self.push_screen,
                            ApprovalModal(title, [str(l) for l in lines]),
                            _done)
            allowed = await fut
            self._activity(Text(
                f"{'✔ allowed' if allowed else '✘ denied'}  {title}",
                style="green" if allowed else "yellow"))
            return allowed

        async def ask_user(self, question: str) -> str:
            """A tool/workflow question: show it in the conversation and
            hand back the next input line."""
            self._write_question(question)
            fut: asyncio.Future = asyncio.get_running_loop().create_future()
            self._pending_reply = fut
            self.query_one("#prompt", Input).focus()
            return await fut

        def submit_background(self, coro) -> asyncio.Task:
            task = asyncio.ensure_future(coro)
            self._bg_tasks.append(task)
            self._bg_tasks = [t for t in self._bg_tasks if not t.done()]
            return task

        # -- commands ---------------------------------------------------------------

        def _handle_command(self, text: str) -> None:
            cmd = text.split()[0].lower()
            if cmd in ("/quit", "/exit"):
                self.exit()
            elif cmd == "/reset":
                if self.state.session_id:
                    self.agent.store.reset(self.state.session_id)
                self.state.session_id = None
                self.query_one("#log", VerticalScroll).remove_children()
                self._refresh_header()
            elif cmd == "/new":
                self.state.session_id = None
                self.query_one("#log", VerticalScroll).remove_children()
                self._refresh_header()
            elif cmd == "/plan":
                task = text[len("/plan"):].strip()
                if not task:
                    self._write_agent("usage: `/plan <task>`")
                    return
                self._write_user(text)
                self._busy = True
                self.run_worker(self._run_plan(task), exclusive=False)
            else:
                # Everything else goes through the shared slash dispatcher;
                # its print() output lands in the activity pane.
                self._write_user(text)
                self.run_worker(self._run_slash(text), exclusive=False)

        async def _run_plan(self, task: str) -> None:
            from botcircuits.agent import Orchestrator
            try:
                orch = Orchestrator(provider=self.provider,
                                    tools=self.agent.tools,
                                    max_tokens=self.agent.max_tokens)
                result = await orch.run(task)
                lines = ["**plan**"]
                lines += [f"{i}. {s}" for i, s in enumerate(result.plan, 1)]
                lines += ["", "**results**"]
                for i, (s, r) in enumerate(zip(result.plan, result.results), 1):
                    lines += [f"{i}. {s}", f"   → {r}"]
                self._write_agent("\n".join(lines))
            except Exception as e:
                self._write_agent(f"**[plan error]** {type(e).__name__}: {e}")
            finally:
                self._busy = False
                self._refresh_usage()

        async def _run_slash(self, text: str) -> None:
            from botcircuits.cli.commands import handle_slash
            try:
                _handled, follow_up = await handle_slash(text, self.agent,
                                                         self.state)
            except SystemExit:
                self.exit()
                return
            except Exception as e:
                self._write_agent(f"**[error]** {type(e).__name__}: {e}")
                return
            if follow_up and not self._busy:
                self._busy = True
                await self._run_turn(follow_up)

        def action_new_session(self) -> None:
            self._handle_command("/new")

    return BotCircuitsTUI()
