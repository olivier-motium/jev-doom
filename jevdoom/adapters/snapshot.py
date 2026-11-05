"""Publish owned state and image together as one tick-tagged value."""

import copy


class SnapshotView:
    def __init__(self):
        self.current = None

    def publish(self, snapshot, frame, tick):
        self.current = (copy.deepcopy(snapshot), bytes(frame), tick)

    def read(self):
        return self.current
