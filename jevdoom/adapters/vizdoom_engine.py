"""Own ViZDoom state and expose frames and terminal results."""

from types import SimpleNamespace
import os
from pathlib import Path
import shutil
import tempfile

import vizdoom as vzd

from jevdoom.core.catalog import BUTTONS, VARIABLES
from jevdoom.ports import Frame, Step

VERSION = vzd.__version__


def fields(source, names):
    return SimpleNamespace(**{name: getattr(source, name) for name in names})


class VizdoomEngine:
    def __init__(self, wad):
        self.wad = wad
        self.game = None
        self.temporary_path = None
        self.record_destination = None
        self.recording_started = False

    def navigation_data(self):
        return self.wad.read_bytes()

    def start(self, map_name, skill, seed, record_path=None, replay_path=None):
        self.close()
        if record_path and replay_path:
            raise ValueError("Cannot record and replay the same episode")
        try:
            return self._start(map_name, skill, seed, record_path, replay_path)
        except BaseException:
            self.close()
            raise

    def _start(self, map_name, skill, seed, record_path, replay_path):
        self.game = game = vzd.DoomGame()
        game.set_doom_game_path(str(self.wad))
        game.set_doom_map(map_name.lower())
        game.set_doom_skill(skill)
        game.set_seed(seed)
        game.set_window_visible(False)
        game.set_sound_enabled(False)
        game.set_screen_format(vzd.ScreenFormat.RGB24)
        game.set_screen_resolution(vzd.ScreenResolution.RES_640X480)
        game.set_labels_buffer_enabled(True)
        game.set_depth_buffer_enabled(True)
        game.set_objects_info_enabled(True)
        game.set_sectors_info_enabled(True)
        game.set_render_hud(True)
        game.set_render_weapon(True)
        game.set_available_buttons([getattr(vzd.Button, b) for b in BUTTONS])
        game.set_mode(vzd.Mode.PLAYER)
        game.set_episode_timeout(35 * 600)
        game.set_map_exit_reward(1)
        game.init()
        if replay_path or record_path:
            # ViZDoom started with a 22-character path but hung at 161 characters on the MacBook.
            fd, name = tempfile.mkstemp(
                prefix="jd-", suffix=".lmp", dir="/tmp" if os.name == "posix" else None
            )
            os.close(fd)
            self.temporary_path = Path(name)
        if replay_path:
            shutil.copyfile(replay_path, self.temporary_path)
            game.replay_episode(str(self.temporary_path))
        elif record_path:
            self.record_destination = Path(record_path)
            game.new_episode(str(self.temporary_path))
            self.recording_started = True
        else:
            game.new_episode()
        return self.frame()

    def variable(self, name):
        return float(self.game.get_game_variable(getattr(vzd.GameVariable, name)))

    def frame(self):
        state = self.game.get_state()
        if state is None:
            return None
        return Frame(
            tic=state.tic,
            variables={name: self.variable(name) for name in VARIABLES},
            labels=tuple(fields(x, ("object_id", "width", "height")) for x in state.labels),
            objects=tuple(
                fields(
                    x,
                    (
                        "name",
                        "id",
                        "position_x",
                        "position_y",
                        "position_z",
                        "velocity_x",
                        "velocity_y",
                    ),
                )
                for x in state.objects
            ),
            sectors=tuple(fields(x, ("floor_height", "ceiling_height")) for x in state.sectors),
            depth_buffer=state.depth_buffer.copy() if state.depth_buffer is not None else None,
            screen_buffer=state.screen_buffer.copy() if state.screen_buffer is not None else None,
        )

    def status(self):
        finished = self.game.is_episode_finished()
        dead = self.game.is_player_dead()
        timeout = self.game.is_episode_timeout_reached()
        reward = self.game.get_last_reward()
        return Step(
            frame=None,
            terminal=finished or dead,
            dead=dead,
            timeout=timeout,
            reward=reward,
            level_done=finished and not dead and not timeout and reward >= 1,
            total_reward=self.game.get_total_reward(),
            exit_reward=self.game.get_map_exit_reward(),
            finished=finished,
        )

    def step(self, buttons):
        self.game.make_action(buttons, 1)
        return self.status()

    def replay_step(self):
        self.game.advance_action(1)
        return self.status()

    def release(self):
        self.game.set_action([0] * len(BUTTONS))

    def close(self):
        try:
            if self.game is not None:
                self.game.close()
                self.game = None
            if self.recording_started:
                shutil.move(str(self.temporary_path), str(self.record_destination))
                self.recording_started = False
        finally:
            if self.temporary_path is not None and not self.recording_started:
                self.temporary_path.unlink(missing_ok=True)
                self.temporary_path = None
                self.record_destination = None
