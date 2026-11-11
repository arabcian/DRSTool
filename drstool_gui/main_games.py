"""Game card and profile ↔ game write flow of MainWindow."""

import logging
from typing import List, Optional

from PySide6.QtWidgets import (
    QDialog, QMessageBox,
)

from .game_card import LinkedGamesDialog
from .links import game_key_for_target, LINKS, plain_title, profile_fp, state_fp
from .toasts import toast

_log = logging.getLogger(__name__)


class GameLinksMixin:
    # ===== Games: game card and profile ↔ game links =====
    def _open_game(self, game):
        self._game_card.set_game(game)
        if self._sidebar_stack.currentIndex() == self.TAB_GAMES:
            self._right_stack.setCurrentIndex(self.PAGE_GAMES)

    def _on_game_written(self, target: dict):
        """A sync tab wrote the editor to a game: remember which profile it was."""
        key = game_key_for_target(target)
        if not key:
            return
        LINKS.record(key, name=plain_title(target.get("title", "")),
                     launcher=str(target.get("kind", "")).capitalize(), target=target,
                     profile=self.settings_manager.get_current_profile(), fp=state_fp(self.capture_state()))

    def _apply_target(self, target: dict) -> tuple:
        w = {"lutris": self._lutris_sync_widget, "steam": self._steam_sync,
             "heroic": self._heroic_sync, "bottles": self._bottles_sync}.get(target.get("kind"))
        if w is None:
            return False, f"no sync tab for “{target.get('kind')}”"
        return w.apply_target(target)

    def _confirm_load_for_write(self, profile: str) -> bool:
        """Writing a profile writes the editor, so the saved profile has to be in it."""
        if self.settings_manager.get_current_profile() == profile and not self._dirty:
            return True
        msg = f"“{profile}” is loaded into the editor first; that is what gets written."
        if self._dirty:
            msg += ("\n\nThe editor has unsaved changes"
                    f" to “{self.settings_manager.get_current_profile()}”. They are replaced "
                    "(Ctrl+Z brings them back).")
        if QMessageBox.question(self, "Load profile", msg, QMessageBox.Ok | QMessageBox.Cancel,
                                QMessageBox.Ok) != QMessageBox.Ok:
            return False
        return self.load_profile_by_name(profile)

    def _editor_matches(self, profile: str) -> bool:
        return self.settings_manager.get_current_profile() == profile and not self._dirty

    def _ask_write_source(self, profile: str, items: List[tuple]) -> Optional[str]:
        """The editor holds something other than the game's linked profile: which one is written?
        -> "editor" | "profile" | None (cancel)."""
        cur = self.settings_manager.get_current_profile()
        what = (f"“{cur}”" + (" (with unsaved changes)" if self._dirty else "")) if cur else "the current settings"
        names = "\n".join(f"  • {n}" for _k, n, _t in items)
        box = QMessageBox(QMessageBox.Question, "Write to game",
                          f"The editor holds {what}, but the game is linked to “{profile}”.\n\n"
                          f"Write to:\n{names}\n\nA timestamped backup of each file is kept. " + (
                              f"Writing the editor links the game to “{cur}” instead." if cur else
                              f"Writing the editor unlinks “{profile}” from the game."), parent=self)
        b_editor = box.addButton("Write the editor as it is", QMessageBox.AcceptRole)
        b_prof = box.addButton(f"Load “{profile}” and write it", QMessageBox.ActionRole)
        box.addButton(QMessageBox.Cancel)
        box.setDefaultButton(b_editor)
        box.exec()
        clicked = box.clickedButton()
        return "editor" if clicked is b_editor else "profile" if clicked is b_prof else None

    def write_profile_to_games(self, profile: Optional[str], items: List[tuple], confirm: bool = True,
                               ask_source: bool = False) -> list:
        """Write a saved profile (None = the editor as it is) to [(game key, name, target)].
        With ask_source, a profile that is not what the editor holds right now can be swapped for
        the editor (Games tab: whatever is loaded stays what gets written).
        -> [(name, ok, message)]"""
        if not items:
            return []
        if profile and profile not in self.settings_manager.get_profiles():
            QMessageBox.warning(self, "Write to game", f"Profile “{profile}” does not exist any more.")
            return []
        loaded = False                               # the editor already holds `profile`
        if profile and ask_source and not self._editor_matches(profile):
            choice = self._ask_write_source(profile, items)
            if choice is None:
                return []
            confirm = False                          # that dialog was the confirmation
            if choice == "editor":
                profile = None
            elif not self._confirm_load_for_write(profile):
                return []
            else:
                loaded = True
        if profile:
            if confirm:
                names = "\n".join(f"  • {n}" for _k, n, _t in items)
                if QMessageBox.question(
                        self, "Write to game",
                        f"Write “{profile}” to:\n\n{names}\n\nA timestamped backup of each file is kept.",
                        QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes) != QMessageBox.Yes:
                    return []
            if not loaded and not self._confirm_load_for_write(profile):
                return []
        elif confirm and QMessageBox.question(
                self, "Write to game",
                "No profile is linked — write the editor's current configuration to:\n\n"
                + "\n".join(f"  • {n}" for _k, n, _t in items) + "\n\nA timestamped backup is kept.",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes) != QMessageBox.Yes:
            return []
        results = []
        for _key, name, target in items:
            ok, msg = self._apply_target(target)
            results.append((name, ok, msg))
        bad = [r for r in results if not r[1]]
        what = f"“{profile}”" if profile else "the editor"
        if not bad:
            toast(f"Wrote {what} to {len(results)} game(s)", "ok", 6000)
        else:
            QMessageBox.warning(
                self, "Write to game",
                f"Wrote {what} to {len(results) - len(bad)} of {len(results)} game(s).\n\n"
                + "\n".join(f"{'✔' if ok else '✖'} {n}: {m}" for n, ok, m in results))
        return results

    def load_profile_for_game(self, name: str):
        if self._dirty and QMessageBox.question(
                self, "Load profile",
                f"Load “{name}”? The editor's unsaved changes are replaced (Ctrl+Z brings them back).",
                QMessageBox.Ok | QMessageBox.Cancel, QMessageBox.Ok) != QMessageBox.Ok:
            return
        if self.load_profile_by_name(name):
            toast(f"Loaded “{name}” into the editor", "ok")

    def open_linked_games(self, name: str):
        """List the games that use a profile and update the chosen ones."""
        profiles = self.settings_manager.get_profiles()
        if name not in profiles:
            return
        dlg = LinkedGamesDialog(name, profiles, self)
        if dlg.exec() == QDialog.Accepted and dlg.chosen:
            self.write_profile_to_games(name, dlg.chosen, confirm=False)

    def _suggest_linked_update(self, name: str):
        for child in self.settings_manager.children_of(name):   # a saved base changes its layers too
            self._suggest_linked_update_one(child)
        self._suggest_linked_update_one(name)

    def _suggest_linked_update_one(self, name: str):
        prof = self.settings_manager.get_profiles().get(name)
        if not isinstance(prof, dict):
            return
        keys = [k for k in LINKS.outdated(name, profile_fp(prof)) if LINKS.get(k).get("target")]
        if keys:
            n = len(keys)
            toast(f"{n} game{'s' if n != 1 else ''} use “{name}” with an older version of it",
                  "info", 15000, (f"Update {n} game{'s' if n != 1 else ''}…", lambda: self.open_linked_games(name)))
