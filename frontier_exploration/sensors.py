import random
from dataclasses import dataclass
from typing import Any

import habitat_sim
import numpy as np
from gym import Space, spaces
from habitat import EmbodiedTask, Sensor, SensorTypes, registry
from habitat.config.default_structured_configs import LabSensorConfig
from habitat.sims.habitat_simulator.habitat_simulator import HabitatSim
from habitat.tasks.nav.nav import TopDownMap
from habitat.utils.visualizations import fog_of_war, maps
from hydra.core.config_store import ConfigStore
from numba import njit
from omegaconf import DictConfig

from frontier_exploration.explorer import detect_frontier_waypoints

STOP, MOVE_FORWARD, TURN_LEFT, TURN_RIGHT = 0, 1, 2, 3


@registry.register_sensor
class FrontierWaypoint(Sensor):
    """Returns a waypoint towards the closest frontier"""

    cls_uuid: str = "frontier_waypoint"

    def __init__(
        self, sim: HabitatSim, config: "DictConfig", *args: Any, **kwargs: Any
    ) -> None:
        super().__init__(sim, config, *args, **kwargs)
        self._sim = sim

        # Extract information from config
        self._config = config
        self._ang_vel = np.deg2rad(config.ang_vel)
        self._area_thresh = config.area_thresh
        self._forward_step_size = config.forward_step_size
        self._fov = config.fov
        self._lin_vel = config.lin_vel
        self._map_resolution = config.map_resolution
        self._success_distance = config.success_distance
        self._turn_angle = np.deg2rad(config.turn_angle)
        self._visibility_dist = config.visibility_dist

        # These public attributes are used by the FrontierExplorationMap measurement
        self.closest_frontier_waypoint = None
        self.top_down_map = None
        self.fog_of_war_mask = None
        self.frontier_waypoints = []

        self._area_thresh_in_pixels = None
        self._visibility_dist_in_pixels = None
        self._agent_position = None
        self._agent_heading = None
        self._curr_ep_id = None
        self._default_dir = False
        self._next_waypoint = None

    def _get_uuid(self, *args: Any, **kwargs: Any) -> str:
        return self.cls_uuid

    def _get_sensor_type(self, *args: Any, **kwargs: Any) -> SensorTypes:
        return SensorTypes.TENSOR

    def _get_observation_space(self, *args: Any, **kwargs: Any) -> Space:
        return spaces.Box(
            low=0,
            high=255,
            shape=(1,),
            dtype=np.uint8,
        )

    @property
    def agent_position(self):
        if self._agent_position is None:
            self._agent_position = self._sim.get_agent_state().position
        return self._agent_position

    @property
    def agent_heading(self):
        if self._agent_heading is None:
            self._agent_heading = TopDownMap.get_polar_angle(self)
        return self._agent_heading

    @property
    def next_waypoint_pixels(self):
        if self._next_waypoint is None:
            return None
        return self._map_coors_to_pixel(self._next_waypoint)

    def get_observation(
        self, task: EmbodiedTask, episode, *args: Any, **kwargs: Any
    ) -> np.ndarray:
        self._agent_position, self._agent_heading = None, None
        if self._curr_ep_id != episode.episode_id:
            self._curr_ep_id = episode.episode_id
            self._reset_maps()  # New episode, reset maps

        updated = self._update_fog_of_war_mask()
        if updated:  # look for new frontiers if the fog of war mask has changed
            self.closest_frontier_waypoint = self._get_frontier_waypoint()
        if self.closest_frontier_waypoint is None:  # no navigable frontiers detected
            return np.array([STOP], dtype=np.int)

        self._next_waypoint = self._get_next_waypoint(self.closest_frontier_waypoint)
        return self._decide_action(self._next_waypoint)

    def _update_fog_of_war_mask(self):
        orig = self.fog_of_war_mask.copy()
        self.fog_of_war_mask = fog_of_war.reveal_fog_of_war(
            self.top_down_map,
            self.fog_of_war_mask,
            self._get_agent_pixel_coords(),
            self.agent_heading,
            fov=self._fov,
            max_line_len=self._visibility_dist_in_pixels,
        )
        updated = not np.array_equal(orig, self.fog_of_war_mask)
        return updated

    def _get_frontier_waypoint(self):
        # Get waypoint to closest frontier
        frontier_waypoints = detect_frontier_waypoints(
            self.top_down_map,
            self.fog_of_war_mask,
            self._area_thresh_in_pixels,
            xy=self._get_agent_pixel_coords(),
        )
        if len(frontier_waypoints) == 0:
            return None
        # frontiers are in (y, x) format, so we need to do some swapping
        self.frontier_waypoints = frontier_waypoints[:, ::-1]
        closest_frontier_waypoint = self._get_closest_waypoint(self.frontier_waypoints)
        return closest_frontier_waypoint

    def _get_next_waypoint(self, frontier_waypoint: np.ndarray):
        shortest_path = habitat_sim.nav.ShortestPath()
        shortest_path.requested_start = self.agent_position
        shortest_path.requested_end = self._pixel_to_map_coors(frontier_waypoint)
        assert self._sim.pathfinder.find_path(shortest_path), "Could not find path!"
        next_waypoint = shortest_path.points[1]
        if shortest_path.geodesic_distance < self._success_distance:
            return None
        return next_waypoint

    def _get_closest_waypoint(self, waypoints: np.ndarray) -> np.ndarray:
        """A* search to find the waypoint that is fastest to reach."""
        sim_waypoints = self._pixel_to_map_coors(waypoints)
        euclidean_dists = np.linalg.norm(sim_waypoints - self.agent_position, axis=1)
        heading_to_waypoints = np.arctan2(
            sim_waypoints[:, 2] - self.agent_position[2],
            sim_waypoints[:, 0] - self.agent_position[0],
        )
        agent_heading = wrap_heading(np.pi / 2.0 - self.agent_heading)
        heading_errors = np.abs(wrap_heading(heading_to_waypoints - agent_heading))
        # Amount of time it would take to reach each waypoint from the current position
        # and heading, ignoring the existence of any obstacles, with point-turn dynamics
        euclidean_completion_times = (
            heading_errors / self._ang_vel + euclidean_dists / self._lin_vel
        )

        sorted_inds = np.argsort(euclidean_completion_times)
        sorted_times = euclidean_completion_times[sorted_inds]
        sorted_waypoints = sim_waypoints[sorted_inds]

        min_cost = np.inf
        closest_waypoint = None
        for idx, sim_waypoint, heuristic, yaw_diff in zip(
            sorted_inds, sorted_waypoints, sorted_times, heading_errors
        ):
            if heuristic > min_cost:
                break
            shortest_path = habitat_sim.nav.ShortestPath()
            shortest_path.requested_start = self.agent_position
            shortest_path.requested_end = sim_waypoint
            if not self._sim.pathfinder.find_path(shortest_path):
                continue
            path = np.array(shortest_path.points)
            cost = shortest_path_completion_time(
                path, self._lin_vel, self._ang_vel, yaw_diff
            )
            if min_cost > cost:
                min_cost = cost
                closest_waypoint = waypoints[idx]
        return closest_waypoint

    def _decide_action(self, next_waypoint: np.ndarray) -> np.ndarray:
        if next_waypoint is None:
            return np.array(
                [TURN_LEFT if self._default_dir else TURN_RIGHT], dtype=np.int
            )

        heading_to_waypoint = np.arctan2(
            next_waypoint[2] - self.agent_position[2],
            next_waypoint[0] - self.agent_position[0],
        )
        agent_heading = wrap_heading(np.pi / 2.0 - self.agent_heading)
        heading_error = wrap_heading(heading_to_waypoint - agent_heading)
        if heading_error > self._turn_angle:
            return np.array([TURN_RIGHT], dtype=np.int)
        elif heading_error < -self._turn_angle:
            return np.array([TURN_LEFT], dtype=np.int)
        return np.array([MOVE_FORWARD], dtype=np.int)

    def _get_agent_pixel_coords(self) -> np.ndarray:
        return self._map_coors_to_pixel(self.agent_position)

    def _convert_meters_to_pixel(self, meters: float) -> int:
        return int(
            meters
            / maps.calculate_meters_per_pixel(self._map_resolution, sim=self._sim)
        )

    def _reset_maps(self):
        self.top_down_map = maps.get_topdown_map_from_sim(
            self._sim,
            map_resolution=self._map_resolution,
            draw_border=False,
        )
        self.fog_of_war_mask = np.zeros_like(self.top_down_map)
        self._area_thresh_in_pixels = self._convert_meters_to_pixel(
            self._area_thresh ** 2
        )
        self._visibility_dist_in_pixels = self._convert_meters_to_pixel(
            self._visibility_dist
        )
        self._default_dir = bool(random.getrandbits(1))
        self._next_waypoint = None

    def _pixel_to_map_coors(self, pixel: np.ndarray) -> np.ndarray:
        if pixel.ndim == 1:
            x, y = pixel
        else:
            x, y = pixel[:, 0], pixel[:, 1]
        realworld_x, realworld_y = maps.from_grid(
            x, y, (self.top_down_map.shape[0], self.top_down_map.shape[1]), self._sim
        )
        if pixel.ndim == 1:
            return self._sim.pathfinder.snap_point(
                [realworld_y, self.agent_position[1], realworld_x]
            )
        snapped = [
            self._sim.pathfinder.snap_point([y, self.agent_position[1], x])
            for y, x in zip(realworld_y, realworld_x)  # noqa
        ]
        return np.array(snapped)

    def _map_coors_to_pixel(self, position) -> np.ndarray:
        a_x, a_y = maps.to_grid(
            position[2],
            position[0],
            (self.top_down_map.shape[0], self.top_down_map.shape[1]),
            sim=self._sim,
        )
        return np.array([a_x, a_y])


@njit
def wrap_heading(heading):
    """Ensures input heading is between -180 an 180; can be float or np.ndarray"""
    return (heading + np.pi) % (2 * np.pi) - np.pi


@njit
def shortest_path_completion_time(path, max_lin_vel, max_ang_vel, yaw_diff):
    time = 0
    cur_pos = path[0]
    cur_yaw = None
    for i in range(1, path.shape[0]):
        target_pos = path[i]
        target_yaw = np.arctan2(target_pos[1] - cur_pos[1], target_pos[0] - cur_pos[0])

        distance = np.sqrt(
            (target_pos[0] - cur_pos[0]) ** 2 + (target_pos[1] - cur_pos[1]) ** 2
        )
        if cur_yaw is not None:
            yaw_diff = np.abs(wrap_heading(target_yaw - cur_yaw))

        lin_time = distance / max_lin_vel
        ang_time = yaw_diff / max_ang_vel
        time += lin_time + ang_time

        cur_pos = target_pos
        cur_yaw = target_yaw

    return time


@dataclass
class FrontierWaypointSensorConfig(LabSensorConfig):
    type: str = FrontierWaypoint.__name__
    # minimum unexplored area (in meters) needed adjacent to a frontier for that
    # frontier to be valid
    ang_vel: float = 10.0  # degrees per second
    area_thresh: float = 3.0  # square meters
    forward_step_size: float = 0.25  # meters
    fov: int = 90
    lin_vel: float = 0.25  # meters per second
    map_resolution: int = 1024
    success_distance: float = 0.1  # meters
    turn_angle: float = 10.0  # degrees
    visibility_dist: float = 5.0  # in meters


cs = ConfigStore.instance()
cs.store(
    package=f"habitat.task.lab_sensors.{FrontierWaypoint.cls_uuid}",
    group="habitat/task/lab_sensors",
    name=f"{FrontierWaypoint.cls_uuid}",
    node=FrontierWaypointSensorConfig,
)
