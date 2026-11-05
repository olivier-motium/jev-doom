"""Own ViZDoom state and expose frames and terminal results."""

from types import SimpleNamespace

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

    def start(self, map_name, skill, seed, record_path=None):
        self.close()
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
        if record_path:
            game.new_episode(str(record_path))
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

    def release(self):
        self.game.set_action([0] * len(BUTTONS))

    def close(self):
        if self.game is not None:
            self.game.close()
            self.game = None
