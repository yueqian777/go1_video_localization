"""Metric-aware grid planning with explicit one-axis Go1 motion primitives."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import heapq
import math

from .free_space import FreeSpaceMap


HEADINGS = {"N": (0, 1), "E": (1, 0), "S": (0, -1), "W": (-1, 0)}
LEFT = {"N": "W", "W": "S", "S": "E", "E": "N"}
RIGHT = {v: k for k, v in LEFT.items()}


class PlanningMode(str, Enum):
    NORMAL = "normal"
    EXPLORATION_CANDIDATE = "exploration_candidate"


@dataclass(frozen=True)
class MotionAction:
    primitive: str
    cell: tuple[int, int] | None = None
    target_heading: str | None = None
    command_id: str | None = None


@dataclass
class GridRoute:
    status: str
    path: list[tuple[int, int, str]]
    actions: list[MotionAction]
    source: str = "free_space_grid"
    reason: str | None = None
    requires_exploration: bool = False


def _turn_actions(current, target):
    actions = []
    while current != target:
        if LEFT[current] == target:
            actions.append(MotionAction("turn_left", target_heading=LEFT[current]))
            current = LEFT[current]
        elif RIGHT[current] == target:
            actions.append(MotionAction("turn_right", target_heading=RIGHT[current]))
            current = RIGHT[current]
        else:
            actions.append(MotionAction("turn_right", target_heading=RIGHT[current]))
            current = RIGHT[current]
    return actions, current


def plan_grid(grid: FreeSpaceMap, start: tuple[int, int], goal: tuple[int, int], *,
              start_heading: str = "E", allow_unknown: bool | None = None,
              planning_mode: PlanningMode = PlanningMode.NORMAL) -> GridRoute:
    if not isinstance(planning_mode, PlanningMode):
        try:
            planning_mode = PlanningMode(planning_mode)
        except ValueError:
            raise ValueError("invalid_planning_mode") from None
    if allow_unknown is not None:
        compatibility_mode = (PlanningMode.EXPLORATION_CANDIDATE
                              if allow_unknown else PlanningMode.NORMAL)
        if planning_mode is not PlanningMode.NORMAL and planning_mode is not compatibility_mode:
            raise ValueError("planning_mode_allow_unknown_conflict")
        planning_mode = compatibility_mode
    if start_heading not in HEADINGS:
        raise ValueError("invalid_start_heading")
    if not grid.is_traversable(start):
        return GridRoute("no_path", [], [], reason="start_not_free")
    if not grid.is_traversable(goal):
        return GridRoute("no_path", [], [], reason="goal_not_free")
    if start == goal:
        return GridRoute("planned", [(start[0], start[1], start_heading)], [], reason="already_at_goal")
    queue = [(0.0, 0.0, 0, start[0], start[1], start_heading)]
    previous = {}
    best = {(start[0], start[1], start_heading): 0.0}
    counter = 0
    saw_unknown = False
    saw_uncertain = False
    while queue:
        _, cost, _, x, y, heading = heapq.heappop(queue)
        state = (x, y, heading)
        if cost != best.get(state):
            continue
        if (x, y) == goal:
            path = [state]
            while state in previous:
                state = previous[state]
                path.append(state)
            path.reverse()
            actions = []
            current_heading = start_heading
            for (ax, ay, _), (bx, by, target_heading) in zip(path, path[1:]):
                turns, current_heading = _turn_actions(current_heading, target_heading)
                actions.extend(turns)
                actions.append(MotionAction("forward", (bx, by), target_heading=target_heading))
            uses_unknown = any(grid.cell(px, py).state.value == "unknown"
                               for px, py, _ in path)
            if uses_unknown and planning_mode is PlanningMode.EXPLORATION_CANDIDATE:
                return GridRoute("exploration_candidate", path, actions,
                                 source="free_space_exploration_candidate",
                                 reason="unknown_cells_in_candidate",
                                 requires_exploration=True)
            return GridRoute("planned", path, actions)
        for target_heading, (dx, dy) in HEADINGS.items():
            nxt = (x + dx, y + dy)
            target_cell = grid.cell(*nxt)
            if target_cell.state.value == "unknown":
                saw_unknown = True
            elif target_cell.state.value == "uncertain":
                saw_uncertain = True
            # OCCUPIED and UNCERTAIN are hard barriers in every mode. The
            # exploration mode can only produce a non-executable candidate
            # through UNKNOWN cells.
            if target_cell.state.value in {"occupied", "uncertain"}:
                continue
            if target_cell.state.value == "unknown" and planning_mode is PlanningMode.NORMAL:
                continue
            turn_count = 0 if target_heading == heading else (2 if RIGHT.get(heading) != target_heading and LEFT.get(heading) != target_heading else 1)
            ncost = cost + 1.0 + 0.4 * turn_count
            nstate = (nxt[0], nxt[1], target_heading)
            if ncost < best.get(nstate, math.inf):
                best[nstate] = ncost
                previous[nstate] = (x, y, heading)
                counter += 1
                heuristic = abs(goal[0] - nxt[0]) + abs(goal[1] - nxt[1])
                heapq.heappush(queue, (ncost + heuristic, ncost, counter, *nstate))
    if saw_uncertain:
        reason = "unknown_or_uncertain_blocked: uncertain"
    elif saw_unknown:
        reason = "unknown_or_uncertain_blocked: unknown"
    else:
        reason = "disconnected_free_space"
    return GridRoute("no_path", [], [], reason=reason)
