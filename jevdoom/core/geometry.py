"""Geometry shared by navigation and motor decisions."""

import math


def dist(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def angle(value):
    return (value + 180) % 360 - 180
