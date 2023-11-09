import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from functools import partial
from typing import List, Optional, Union

from gi.repository import Gtk, Gdk, GLib, Pango

PROMPT_ACTION_PREFIX = '// action:prompt_'
BUTTON_ARGS_RE = re.compile(r'(?P<label>[^|]+)(?:\|(?P<action>[^|]+)(?:\|(?P<color>[^|]+))?)?')


class PrompState(Enum):
    IDLE = 0
    BUILDING = 1
    ACTIVE = 2

class PromptActions(Enum):
    BEGIN = 'begin'
    BUTTON = 'button'
    TEXT = 'text'
    BUTTON_GROUP_START = 'button_group_start'
    BUTTON_GROUP_END = 'button_group_end'
    SHOW = 'show'
    CLOSE = 'close'


ACTION_STATE_MAP = {
    PromptActions.BEGIN: {PrompState.IDLE},
    PromptActions.BUTTON: {PrompState.BUILDING},
    PromptActions.TEXT: {PrompState.BUILDING},
    PromptActions.BUTTON_GROUP_START: {PrompState.BUILDING},
    PromptActions.BUTTON_GROUP_END: {PrompState.BUILDING},
    PromptActions.SHOW: {PrompState.BUILDING, PrompState.ACTIVE},
    PromptActions.CLOSE: None,  # None means all states allowed
}


@dataclass
class Button:
    label: str
    action: Optional[str] = None
    color: Optional[str] = None

    @classmethod
    def from_args(cls, args):
        match = BUTTON_ARGS_RE.match(args)
        if not match:
            raise ValueError(f"Invalid button arguments: {args}")

        label, action, color = match.groups()
        return cls(label, action, color)


@dataclass
class ButtonGroup:
    buttons: List[Button] = field(default_factory=list)


@dataclass
class Prompt:
    title: str
    contents: List[Union[Button, ButtonGroup, str]] = field(default_factory=list)
    footer_buttons: List[Button] = field(default_factory=list)

    # Holds a ButtonGroup instance while the group is being built
    button_group_builder: Optional[ButtonGroup] = None


class ActionPromptHandler:
    def __init__(self, screen):
        self.screen = screen
        self.state = PrompState.IDLE
        self.prompt_window = None
        self.prompt = None

    def process_update(self, data):
        logging.info(f"APH: {data}")
        remainder, prefix, action_args = data.partition(PROMPT_ACTION_PREFIX)
        logging.info(f"APH: {remainder}, {prefix}, {action_args}")
        if remainder or prefix != PROMPT_ACTION_PREFIX:
            # Not a prompt action
            return
        action_str, _, args = action_args.partition(' ')
        logging.info(f"APH: {action_str}, {args}")

        try:
            action = PromptActions(action_str.lower())
        except ValueError:
            logging.info(f"APH: Unknown action: {action_str}")
            return

        allowed_states = ACTION_STATE_MAP[action]
        if allowed_states and self.state not in allowed_states:
            logging.info(f"APH: Wrong state for '{action.value}': {self.state}")
            return

        getattr(self, f"handle_{action.value}")(args)
        logging.info(f"APH: state: {self.state}, {self.prompt}")

    def handle_begin(self, args):
        self.prompt = Prompt(title=args)
        self.state = PrompState.BUILDING

    def handle_button(self, args):
        try:
            button = Button.from_args(args)
        except ValueError as e:
            logging.error(f"APH: Invalid button arguments: {args} -> {e}")

        if self.prompt.button_group_builder:
            self.prompt.button_group_builder.buttons.append(button)
        else:
            self.prompt.contents.append(button)

    def handle_footer_button(self, args):
        try:
            self.prompt.footer_buttons.append(Button.from_args(args))
        except ValueError as e:
            logging.error(f"APH: Invalid button arguments: {args} -> {e}")

    def handle_text(self, args):
        if self.state is not PrompState.BUILDING:
            logging.info(f"APH: Wrong state for 'text': {self.state}")
            return
        self.prompt.contents.append(args)

    def handle_button_group_start(self, args):
        self.prompt.button_group_builder = ButtonGroup()

    def handle_button_group_end(self, args):
        if not self.prompt.button_group_builder:
            logging.error(f"APH: No button group to end")
            return
        self.prompt.contents.append(self.prompt.button_group_builder)
        self.prompt.button_group_builder = None

    def handle_show(self, args):
        self.show_prompt()
        self.state = PrompState.ACTIVE

    def handle_close(self, args):
        if self.state is PrompState.BUILDING:
            self.prompt = None
            self.state = PrompState.IDLE
            return
        elif self.state is PrompState.ACTIVE:
            self.dismiss_prompt()
            self.state = PrompState.IDLE

    def show_prompt(self):
        self.screen.close_screensaver()

        if self.state is PrompState.ACTIVE:
            logging.info(f"APH.show_prompt: state: active, prompt_window: {self.prompt_window}")
            return

        def _response(dialog, response):
            self.dismiss_prompt()

        content = Gtk.Box(spacing=6)
        content.set_orientation(Gtk.Orientation.VERTICAL)

        label = Gtk.Label()
        label.set_markup(f"<big><b>{self.prompt.title}</b></big>")
        content.pack_start(label, True, True, 0)

        for choice in self.prompt.contents:
            if isinstance(choice, str):
                label = Gtk.Label()
                label.set_markup(choice)
                content.pack_start(label, True, True, 0)
            elif isinstance(choice, ButtonGroup):
                group = Gtk.Box(spacing=6)
                group.set_orientation(Gtk.Orientation.HORIZONTAL)
                for group_button in choice.buttons:
                    button = Gtk.Button(label=group_button.label)
                    button.set_hexpand(True)
                    button.set_vexpand(True)
                    button.connect("clicked", partial(self.choice_clicked, group_button))
                    button.get_style_context().add_class("message_popup_button")
                    group.pack_start(button, True, True, 0)
                content.pack_start(group, True, True, 0)
            elif isinstance(choice, Button):
                button = Gtk.Button(label=choice.label)
                button.set_hexpand(True)
                button.set_vexpand(True)
                button.connect("clicked", partial(self.choice_clicked, choice))
                button.get_style_context().add_class("message_popup_button")
                content.pack_start(button, True, True, 0)

        self.prompt_window = self.screen.gtk.Dialog(
            self.prompt.title,
            [{"name": "Cancel", "response": 0}],
            content,
            _response
        )
        # Make sure our internal state is consistent with the dialog state
        self.prompt_window.connect("destroy", self.dismiss_prompt)

    def dismiss_prompt(self, *_args):
        logging.info(f"APH.dismiss_prompt")
        if self.prompt_window is None:
            return
        self.screen.gtk.remove_dialog(self.prompt_window)
        self.screen._ws.send_method(
            "printer.gcode.script",
            {"script": "RESPOND TYPE=command MSG=action:prompt_close"},
        )
        self.prompt_window = None
        self.prompt = None
        self.state = PrompState.IDLE

    def choice_clicked(self, choice, button):
        self.screen.gtk.Button_busy(button, True)
        self.screen._ws.send_method(
            "printer.gcode.script",
            {"script": choice.action if choice.action else choice.label},
            self.dismiss_prompt
        )
        logging.info(f"APH.choice_clicked: {choice}, {button}")


def test_action_prompt_handler():
    handler = ActionPromptHandler()
    handler.process_update('// action:prompt_begin Hello test prompt')
    handler.process_update('// action:prompt_choice Yes')
    handler.process_update('// action:prompt_choice No thanks')
    handler.process_update('// action:prompt_show')
    handler.process_update('// action:prompt_end')
    handler.process_update('// action:prompt_begin Test')
    handler.process_update('// action:prompt_end')
    handler.process_update('// action:prompt_choice No')
    handler.process_update('// action:prompt_show')
    handler.process_update('// action:prompt_end')


if __name__ == "__main__":
    test_action_prompt_handler()
