from __future__ import annotations

import hashlib
import math
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import pygame

from src.common.config import as_int, read_simple_conf
from src.common.geometry import point_in_polygon
from src.common.io import load_car, load_track, save_track
from src.common.models import CarBehaviorProfile, CarConfig, CarLearningState, CarRaceMemory, CarRaceOutcome, CarRuntimeState
from src.common.physics import update_car_state
from src.common.ui import create_default_font, draw_dropdown_menus, draw_file_picker, draw_lines, menu_action_at

try:
    from PIL import Image
except Exception:  # pragma: no cover - optional dependency fallback
    Image = None


@dataclass
class SimCar:
    instance_name: str
    source_file: str
    config: CarConfig
    state: CarRuntimeState
    start_pose: tuple[float, float, float]
    behavior: CarBehaviorProfile = field(default_factory=CarBehaviorProfile)
    learning: CarLearningState = field(default_factory=CarLearningState)
    memory: CarRaceMemory = field(default_factory=CarRaceMemory)
    race_elapsed: float = 0.0
    speed_accum: float = 0.0
    speed_samples: int = 0
    barrier_hits: int = 0
    best_lap_seconds: float = 0.0
    last_lap_seconds: float = 0.0
    lap_start_time: float = 0.0
    last_lap_damage_checkpoint: float = 0.0
    pass_side_bias: float = 0.0
    pace_bias: float = 1.0
    steer_bias: float = 1.0


@dataclass
class StatsDropdownState:
    header_rect: pygame.Rect = field(default_factory=lambda: pygame.Rect(0, 0, 0, 0))
    row_rects: list[tuple[int, pygame.Rect]] = field(default_factory=list)
    open: bool = False


def load_latest(path: Path, suffix: str):
    files = sorted(path.glob(f"*{suffix}"))
    if not files:
        return None
    return files[-1]


def _personality_unit(seed_text: str, salt: str) -> float:
    digest = hashlib.sha256(f"{seed_text}|{salt}".encode("utf-8")).digest()
    value = int.from_bytes(digest[:8], "big")
    return value / float((1 << 64) - 1)


def _build_personality(instance_name: str, car: CarConfig) -> tuple[CarBehaviorProfile, CarLearningState, float, float, float]:
    base = f"{instance_name}:{car.name}:{car.mass:.1f}:{car.max_speed:.1f}"

    def span(salt: str, low: float, high: float) -> float:
        t = _personality_unit(base, salt)
        return low + (high - low) * t

    profile = CarBehaviorProfile(
        speed_priority=span("speed_priority", 0.85, 1.25),
        lap_improvement_priority=span("lap_priority", 0.75, 1.2),
        damage_avoidance_priority=span("damage_avoid", 0.55, 1.05),
        keep_nose_forward_priority=span("nose_forward", 0.55, 1.1),
        avoid_slowdown_priority=span("avoid_slow", 0.65, 1.15),
        barrier_avoidance_priority=span("barrier_avoid", 0.55, 1.15),
        risk_tolerance=span("risk", 0.35, 0.9),
    )

    learning = CarLearningState(
        target_speed_bias=span("target_speed_bias", 0.9, 1.18),
        steering_aggression=span("steer_aggression", 0.82, 1.18),
        safety_bias=span("safety_bias", 0.86, 1.18),
    )

    pass_side_bias = span("pass_side", -1.0, 1.0)
    pace_bias = span("pace_bias", 0.9, 1.13)
    steer_bias = span("steer_bias", 0.9, 1.12)
    return profile, learning, pass_side_bias, pace_bias, steer_bias


def _ccw_spawn_heading(centerline: list[tuple[float, float]], index: int) -> float:
    if len(centerline) < 2:
        return -math.pi / 2

    cx = sum(p[0] for p in centerline) / len(centerline)
    cy = sum(p[1] for p in centerline) / len(centerline)

    curr = centerline[index]
    prev_pt = centerline[(index - 1) % len(centerline)]
    next_pt = centerline[(index + 1) % len(centerline)]

    candidates = [
        (next_pt[0] - curr[0], next_pt[1] - curr[1]),
        (prev_pt[0] - curr[0], prev_pt[1] - curr[1]),
    ]

    rx = curr[0] - cx
    ry = curr[1] - cy
    best = candidates[0]
    best_cross = float("inf")
    for tx, ty in candidates:
        cross = rx * ty - ry * tx
        if cross < best_cross:
            best_cross = cross
            best = (tx, ty)

    return math.atan2(best[1], best[0])


def spawn_state(track, car: CarConfig) -> CarRuntimeState:
    x, y, w, h = track.start_grid
    spawn_x = x + w / 2
    spawn_y = y + h / 2

    centerline = [
        (
            (track.outer_points[i][0] + track.inner_points[i][0]) * 0.5,
            (track.outer_points[i][1] + track.inner_points[i][1]) * 0.5,
        )
        for i in range(min(len(track.outer_points), len(track.inner_points)))
    ]

    heading = -math.pi / 2
    if len(centerline) >= 2:
        nearest_index = 0
        nearest_dist = float("inf")
        for i, pt in enumerate(centerline):
            dx = pt[0] - spawn_x
            dy = pt[1] - spawn_y
            dist = dx * dx + dy * dy
            if dist < nearest_dist:
                nearest_dist = dist
                nearest_index = i
        heading = _ccw_spawn_heading(centerline, nearest_index)

    return CarRuntimeState(
        x=spawn_x,
        y=spawn_y,
        heading_radians=heading,
        speed=0.0,
        tire_health=car.starting_tire_health,
        fuel=car.starting_fuel,
        damage=0.0,
    )


def _smooth_loop(points: list[tuple[float, float]], piece_types: list[str]) -> list[tuple[float, float]]:
    if len(points) < 4 or len(piece_types) != len(points):
        return points

    out: list[tuple[float, float]] = []
    corner_ratio = 0.33
    for i, curr in enumerate(points):
        prev = points[(i - 1) % len(points)]
        nxt = points[(i + 1) % len(points)]
        if piece_types[i] != "curve":
            out.append(curr)
            continue

        entry = (
            curr[0] + (prev[0] - curr[0]) * corner_ratio,
            curr[1] + (prev[1] - curr[1]) * corner_ratio,
        )
        exit = (
            curr[0] + (nxt[0] - curr[0]) * corner_ratio,
            curr[1] + (nxt[1] - curr[1]) * corner_ratio,
        )
        out.append(entry)
        for step in range(1, 7):
            t = step / 7.0
            omt = 1.0 - t
            bezier = (
                omt * omt * entry[0] + 2 * omt * t * curr[0] + t * t * exit[0],
                omt * omt * entry[1] + 2 * omt * t * curr[1] + t * t * exit[1],
            )
            out.append(bezier)
        out.append(exit)
    return out


def draw_track(surface: pygame.Surface, track) -> None:
    outer_types = [piece.piece_type for piece in track.outer_pieces]
    inner_types = [piece.piece_type for piece in track.inner_pieces]
    outer_path = _smooth_loop(track.outer_points, outer_types)
    inner_path = _smooth_loop(track.inner_points, inner_types)

    pygame.draw.polygon(surface, (10, 10, 10), outer_path)
    pygame.draw.polygon(surface, (42, 145, 75), inner_path)
    pygame.draw.lines(surface, (220, 220, 220), True, outer_path, 3)
    pygame.draw.lines(surface, (220, 220, 220), True, inner_path, 3)
    pygame.draw.rect(surface, (230, 190, 40), track.start_grid)


def draw_car(surface: pygame.Surface, state: CarRuntimeState, car: CarConfig) -> None:
    body = pygame.Surface((int(car.length), int(car.width)), pygame.SRCALPHA)
    body.fill(car.body_color)
    nose = pygame.Rect(int(car.length * 0.7), 0, int(car.length * 0.3), int(car.width))
    pygame.draw.rect(body, car.nose_color, nose)
    rotated = pygame.transform.rotate(body, -math.degrees(state.heading_radians))
    rect = rotated.get_rect(center=(state.x, state.y))
    surface.blit(rotated, rect)


def _car_draw_rect(state: CarRuntimeState, car: CarConfig) -> pygame.Rect:
    body = pygame.Surface((int(car.length), int(car.width)), pygame.SRCALPHA)
    rotated = pygame.transform.rotate(body, -math.degrees(state.heading_radians))
    return rotated.get_rect(center=(state.x, state.y))


def _reset_for_race(sim_car: SimCar) -> None:
    state = sim_car.state
    car = sim_car.config
    state.x, state.y, state.heading_radians = sim_car.start_pose
    state.speed = 0.0
    state.vx = 0.0
    state.vy = 0.0
    state.yaw_rate = 0.0
    state.tire_health = car.starting_tire_health
    state.fuel = car.starting_fuel
    state.damage = 0.0
    state.state = "stopped"
    state.laps = 0
    state.left_start_zone = False
    state.cumulative_angle = 0.0
    state.nav_last_index = -1
    state.nav_stall_frames = 0
    state.wall_contact_frames = 0
    state.distance_traveled = 0.0
    state.last_lap_distance = 0.0

    sim_car.race_elapsed = 0.0
    sim_car.speed_accum = 0.0
    sim_car.speed_samples = 0
    sim_car.barrier_hits = 0
    sim_car.best_lap_seconds = 0.0
    sim_car.last_lap_seconds = 0.0
    sim_car.lap_start_time = 0.0
    sim_car.last_lap_damage_checkpoint = 0.0


def draw_crash_fallback(surface: pygame.Surface, state: CarRuntimeState, car: CarConfig) -> None:
    cx = int(state.x)
    cy = int(state.y)
    w = max(12, int(car.length * 0.35))
    h = max(16, int(car.width * 1.4))
    flame_points = [
        (cx, cy - h),
        (cx - w, cy + h // 3),
        (cx - w // 3, cy + h),
        (cx + w // 3, cy + h),
        (cx + w, cy + h // 3),
    ]
    core_points = [
        (cx, cy - int(h * 0.55)),
        (cx - int(w * 0.45), cy + int(h * 0.2)),
        (cx, cy + int(h * 0.65)),
        (cx + int(w * 0.45), cy + int(h * 0.2)),
    ]
    pygame.draw.polygon(surface, (255, 120, 35, 230), flame_points)
    pygame.draw.polygon(surface, (255, 220, 90, 230), core_points)


def _load_crash_overlay(project_dir: Path) -> pygame.Surface | None:
    def _load_with_pillow(path: Path) -> pygame.Surface | None:
        if Image is None:
            return None
        try:
            image = Image.open(path).convert("RGBA")
            data = image.tobytes()
            return pygame.image.fromstring(data, image.size, "RGBA").convert_alpha()
        except Exception:
            return None

    image_dir = project_dir / "images"
    for name in ("flame_affect_car.png", "flame_effect_car.png"):
        path = image_dir / name
        if path.exists():
            try:
                return pygame.image.load(path.as_posix()).convert_alpha()
            except pygame.error:
                pillow_surface = _load_with_pillow(path)
                if pillow_surface is not None:
                    return pillow_surface

                try:
                    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                        tmp_path = Path(tmp.name)
                    subprocess.run(
                        ["sips", "-s", "format", "png", path.as_posix(), "--out", tmp_path.as_posix()],
                        check=True,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    try:
                        pygame_surface = pygame.image.load(tmp_path.as_posix()).convert_alpha()
                        return pygame_surface
                    except pygame.error:
                        return _load_with_pillow(tmp_path)
                    finally:
                        tmp_path.unlink(missing_ok=True)
                except Exception:
                    pass
    return None


def _wrap_angle(angle: float) -> float:
    while angle > math.pi:
        angle -= 2 * math.pi
    while angle < -math.pi:
        angle += 2 * math.pi
    return angle


def _build_centerline(track) -> list[tuple[float, float]]:
    count = min(len(track.outer_points), len(track.inner_points))
    base = [
        (
            (track.outer_points[i][0] + track.inner_points[i][0]) * 0.5,
            (track.outer_points[i][1] + track.inner_points[i][1]) * 0.5,
        )
        for i in range(count)
    ]
    if len(base) < 2:
        return base

    dense: list[tuple[float, float]] = []
    subdivisions = 8
    for i in range(len(base)):
        a = base[i]
        b = base[(i + 1) % len(base)]
        for step in range(subdivisions):
            t = step / subdivisions
            dense.append((a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t))
    return dense


def _closest_centerline_index(state: CarRuntimeState, centerline: list[tuple[float, float]]) -> int:
    best_index = 0
    best_dist = float("inf")
    for index, point in enumerate(centerline):
        dx = point[0] - state.x
        dy = point[1] - state.y
        dist = dx * dx + dy * dy
        if dist < best_dist:
            best_dist = dist
            best_index = index
    return best_index


def _turn_severity(centerline: list[tuple[float, float]], index: int) -> float:
    if len(centerline) < 3:
        return 0.0
    prev_pt = centerline[(index - 1) % len(centerline)]
    curr_pt = centerline[index]
    next_pt = centerline[(index + 1) % len(centerline)]

    in_angle = math.atan2(curr_pt[1] - prev_pt[1], curr_pt[0] - prev_pt[0])
    out_angle = math.atan2(next_pt[1] - curr_pt[1], next_pt[0] - curr_pt[0])
    delta = abs(_wrap_angle(out_angle - in_angle))
    return min(1.0, delta / math.pi)


def _blend_headings(a: float, b: float, weight_b: float) -> float:
    weight_b = max(0.0, min(1.0, weight_b))
    weight_a = 1.0 - weight_b
    x = weight_a * math.cos(a) + weight_b * math.cos(b)
    y = weight_a * math.sin(a) + weight_b * math.sin(b)
    return math.atan2(y, x)


def autonomous_controls(
    state: CarRuntimeState,
    car: CarConfig,
    track,
    behavior: CarBehaviorProfile,
    learning: CarLearningState,
    traffic: list[tuple[float, float, float, float]],
    pass_side_bias: float,
    pace_bias: float,
    steer_bias: float,
) -> tuple[float, float, float]:
    centerline = _build_centerline(track)
    if len(centerline) < 2:
        return 0.0, 0.0, 0.0

    lane_width_samples = [
        math.hypot(track.outer_points[i][0] - track.inner_points[i][0], track.outer_points[i][1] - track.inner_points[i][1])
        for i in range(min(len(track.outer_points), len(track.inner_points)))
    ]
    avg_lane_width = sum(lane_width_samples) / max(1, len(lane_width_samples))

    nearest = _closest_centerline_index(state, centerline)
    if state.nav_last_index == nearest:
        state.nav_stall_frames += 1
    else:
        state.nav_last_index = nearest
        state.nav_stall_frames = 0

    heading_vec = (math.cos(state.heading_radians), math.sin(state.heading_radians))
    curr = centerline[nearest]
    plus = centerline[(nearest + 1) % len(centerline)]
    minus = centerline[(nearest - 1) % len(centerline)]
    plus_vec = (plus[0] - curr[0], plus[1] - curr[1])
    minus_vec = (minus[0] - curr[0], minus[1] - curr[1])
    dot_plus = heading_vec[0] * plus_vec[0] + heading_vec[1] * plus_vec[1]
    dot_minus = heading_vec[0] * minus_vec[0] + heading_vec[1] * minus_vec[1]
    if state.nav_direction not in (-1, 1):
        state.nav_direction = 1 if dot_plus >= dot_minus else -1
    forward_step = state.nav_direction

    speed_ratio = min(1.0, abs(state.speed) / max(car.max_speed, 1.0))
    severity_now = _turn_severity(centerline, nearest)
    lookahead = 1 + int(speed_ratio * 4.0 + severity_now * 2.0)
    lookahead = min(max(lookahead, 1), max(1, len(centerline) // 3))
    target = centerline[(nearest + forward_step * lookahead) % len(centerline)]
    target_next = centerline[(nearest + forward_step * (lookahead + 1)) % len(centerline)]

    if state.nav_stall_frames > 120:
        path_heading_recover = math.atan2(target_next[1] - target[1], target_next[0] - target[0])
        state.heading_radians = _blend_headings(state.heading_radians, path_heading_recover, 0.55)
        state.speed = max(state.speed, 10.0)
        state.nav_last_index = nearest
        state.nav_stall_frames = 0

    nearest_point = centerline[nearest]
    dist_to_center = math.hypot(nearest_point[0] - state.x, nearest_point[1] - state.y)
    local_track_width = max(1.0, avg_lane_width)
    off_center = dist_to_center > local_track_width * 0.35

    target_heading = math.atan2(target[1] - state.y, target[0] - state.x)
    path_heading = math.atan2(target_next[1] - target[1], target_next[0] - target[0])
    dist_to_target = math.hypot(target[0] - state.x, target[1] - state.y)
    turn_in_distance = max(local_track_width * 2.1, 120.0)
    path_weight = 0.35 if dist_to_target > turn_in_distance else 0.78
    desired_heading = _blend_headings(target_heading, path_heading, path_weight)
    if off_center:
        recover_point = centerline[(nearest + forward_step) % len(centerline)]
        recover_heading = math.atan2(recover_point[1] - state.y, recover_point[0] - state.x)
        desired_heading = _blend_headings(desired_heading, recover_heading, 0.35)

    severity_ahead = _turn_severity(centerline, (nearest + forward_step * lookahead) % len(centerline))

    # Traffic-aware adjustment: avoid cars ahead while staying stable through corners.
    forward_x = math.cos(state.heading_radians)
    forward_y = math.sin(state.heading_radians)
    avoid_steer = 0.0
    slowdown_factor = 1.0
    emergency_brake = False
    nearest_ahead = float("inf")
    traffic_pressure = 0.0
    for ox, oy, other_radius, other_speed in traffic:
        rel_x = ox - state.x
        rel_y = oy - state.y
        ahead = rel_x * forward_x + rel_y * forward_y
        if ahead <= 0.0:
            continue
        if ahead > 180.0:
            continue

        side = rel_x * (-forward_y) + rel_y * forward_x
        lateral_limit = max(20.0, (car.width + other_radius * 1.4) * 0.9)
        if abs(side) > lateral_limit:
            continue

        nearest_ahead = min(nearest_ahead, ahead)
        if abs(side) < 3.0:
            side_sign = 1.0 if pass_side_bias >= 0.0 else -1.0
        else:
            side_sign = -1.0 if side >= 0.0 else 1.0
        proximity = max(0.0, 1.0 - ahead / 180.0)
        traffic_pressure = max(traffic_pressure, proximity)

        steer_gain = 0.25 + proximity * 0.45
        if severity_now > 0.16 or severity_ahead > 0.18:
            steer_gain *= 0.3
        avoid_steer += side_sign * steer_gain

        closing_speed = max(0.0, state.speed - other_speed)
        if closing_speed > 4.0:
            slowdown_factor = min(slowdown_factor, max(0.22, 1.0 - proximity * 0.9))
        if ahead < 54.0 and closing_speed > 2.0:
            emergency_brake = True

    avoid_steer = max(-0.55, min(0.55, avoid_steer))

    heading_error = _wrap_angle(desired_heading - state.heading_radians)
    segment_heading = math.atan2(target[1] - nearest_point[1], target[0] - nearest_point[0])
    turn_feedforward = _wrap_angle(path_heading - segment_heading)
    steering_gain = (3.2 + severity_now * 2.0) * learning.steering_aggression * steer_bias
    if off_center:
        steering_gain += 1.4
    steering_cmd = heading_error * steering_gain + turn_feedforward * 0.9
    steering_cmd += avoid_steer
    steering = max(-1.0, min(1.0, steering_cmd))

    severity = max(severity_now, severity_ahead)
    in_turn = severity > 0.12
    base_target = car.max_speed * (0.09 + (1.0 - severity) * 0.11)
    angle_factor = max(0.12, 1.0 - (abs(heading_error) / math.pi) * 1.6)

    speed_priority_scale = 1.0 + (behavior.speed_priority + behavior.avoid_slowdown_priority) * 0.08
    speed_priority_scale *= pace_bias
    target_speed = min(car.max_speed, base_target * angle_factor * speed_priority_scale * learning.target_speed_bias)
    target_speed *= slowdown_factor
    if nearest_ahead < float("inf"):
        follow_cap = max(12.0, nearest_ahead * 0.45)
        if severity > 0.16:
            follow_cap *= 0.8
        target_speed = min(target_speed, follow_cap)

    damage_factor = max(0.35, 1.0 - (state.damage / 160.0) * learning.safety_bias)
    target_speed *= damage_factor
    if off_center:
        target_speed = min(target_speed, 30.0)

    # Keep momentum through corners to avoid stop-and-go behavior.
    corner_carry_speed = max(18.0, min(34.0, car.max_speed * 0.075))
    if in_turn:
        target_speed = max(target_speed, corner_carry_speed)

    forward_speed = max(0.0, state.speed)
    speed_error = target_speed - forward_speed
    throttle = 0.0
    brake = 0.0
    coast_phase = False

    # True coast phase: release both pedals when speed is close to target,
    # especially in corners, to carry inertia smoothly.
    if not emergency_brake:
        if in_turn and abs(speed_error) <= 4.5 and abs(heading_error) <= 0.5 and nearest_ahead > 72.0:
            coast_phase = True
        elif not in_turn and abs(speed_error) <= 3.0 and nearest_ahead > 80.0:
            coast_phase = True

    if not coast_phase:
        if speed_error > 2.5:
            throttle = 1.0
    overspeed = forward_speed - target_speed

    safety_turn_guard = 1.0 + behavior.barrier_avoidance_priority * 0.25 + (learning.safety_bias - 1.0) * 0.3
    if not coast_phase:
        if overspeed > 12.0:
            brake = max(brake, min(0.85, overspeed / 22.0))
        if in_turn and severity_ahead * safety_turn_guard > 0.24 and forward_speed > 26.0:
            brake = max(brake, 0.55)
        if in_turn and forward_speed > 24.0 and abs(heading_error) > 0.55:
            brake = max(brake, 0.5)
        if in_turn and off_center and forward_speed > 28.0:
            brake = max(brake, 0.65)
        if state.damage > 55.0 and forward_speed > target_speed:
            brake = max(brake, 0.55)
        if nearest_ahead < 64.0 and forward_speed > target_speed + 4.0:
            brake = max(brake, 0.6)
        if in_turn and traffic_pressure > 0.35 and forward_speed > 22.0:
            brake = max(brake, 0.5)
    if emergency_brake:
        brake = 1.0

    return throttle, brake, steering


def _centerline_length(track) -> float:
    count = min(len(track.outer_points), len(track.inner_points))
    if count < 2:
        return 0.0
    points = [
        (
            (track.outer_points[i][0] + track.inner_points[i][0]) * 0.5,
            (track.outer_points[i][1] + track.inner_points[i][1]) * 0.5,
        )
        for i in range(count)
    ]
    total = 0.0
    for i in range(len(points)):
        a = points[i]
        b = points[(i + 1) % len(points)]
        total += math.hypot(b[0] - a[0], b[1] - a[1])
    return total


def update_lap_counter(state: CarRuntimeState, track) -> None:

    in_start = pygame.Rect(track.start_grid).collidepoint(state.x, state.y)
    if not in_start:
        state.left_start_zone = True

    if in_start and state.left_start_zone:
        lap_len = _centerline_length(track)
        min_lap_distance = max(160.0, lap_len * 0.55)
        if state.distance_traveled - state.last_lap_distance >= min_lap_distance:
            state.laps += 1
            state.left_start_zone = False
            state.cumulative_angle = 0.0
            state.last_lap_distance = state.distance_traveled


def _make_unique_car_name(base_name: str, cars: list[SimCar]) -> str:
    existing = {entry.instance_name for entry in cars}
    if base_name not in existing:
        return base_name
    suffix = 2
    while True:
        candidate = f"{base_name}_{suffix}"
        if candidate not in existing:
            return candidate
        suffix += 1


def _is_on_surface(track, x: float, y: float) -> bool:
    return point_in_polygon((x, y), track.outer_points) and not point_in_polygon((x, y), track.inner_points)


def _car_collision_radius(car: CarConfig) -> float:
    return max(8.0, math.hypot(car.length, car.width) * 0.34)


def _car_overlaps_any(sim_cars: list[SimCar], idx: int, x: float, y: float, car: CarConfig) -> bool:
    radius = _car_collision_radius(car)
    for other_idx, other in enumerate(sim_cars):
        if other_idx == idx:
            continue
        other_radius = _car_collision_radius(other.config)
        min_dist = radius + other_radius + 4.0
        dx = other.state.x - x
        dy = other.state.y - y
        if dx * dx + dy * dy < min_dist * min_dist:
            return True
    return False


def _find_open_pose(
    track,
    sim_cars: list[SimCar],
    idx: int,
    car: CarConfig,
    base_pose: tuple[float, float, float],
) -> tuple[float, float, float]:
    bx, by, heading = base_pose
    if _is_on_surface(track, bx, by) and not _car_overlaps_any(sim_cars, idx, bx, by, car):
        return (bx, by, heading)

    for ring in range(1, 10):
        radius = ring * 18.0
        for step in range(16):
            angle = (step / 16.0) * math.tau
            px = bx + math.cos(angle) * radius
            py = by + math.sin(angle) * radius
            if not _is_on_surface(track, px, py):
                continue
            if _car_overlaps_any(sim_cars, idx, px, py, car):
                continue
            return (px, py, heading)
    return (bx, by, heading)


def _resolve_car_overlaps(sim_cars: list[SimCar]) -> None:
    for i in range(len(sim_cars)):
        a = sim_cars[i]
        for j in range(i + 1, len(sim_cars)):
            b = sim_cars[j]
            ra = _car_collision_radius(a.config)
            rb = _car_collision_radius(b.config)
            min_dist = ra + rb
            dx = b.state.x - a.state.x
            dy = b.state.y - a.state.y
            dist_sq = dx * dx + dy * dy
            if dist_sq >= min_dist * min_dist:
                continue

            dist = math.sqrt(dist_sq) if dist_sq > 1e-6 else 1e-3
            nx = dx / dist
            ny = dy / dist
            overlap = min_dist - dist
            push = overlap * 0.5 + 0.5

            a.state.x -= nx * push
            a.state.y -= ny * push
            b.state.x += nx * push
            b.state.y += ny * push

            a.state.speed *= 0.92
            b.state.speed *= 0.92
            a.state.damage = min(100.0, a.state.damage + 0.3)
            b.state.damage = min(100.0, b.state.damage + 0.3)


def _serialize_car_starts(sim_cars: list[SimCar]) -> list[dict[str, object]]:
    return [
        {
            "instance_name": entry.instance_name,
            "car_file": entry.source_file,
            "start_pose": [entry.start_pose[0], entry.start_pose[1], entry.start_pose[2]],
        }
        for entry in sim_cars
    ]


def _finalize_race_outcome(sim_car: SimCar) -> None:
    avg_speed = 0.0
    if sim_car.speed_samples > 0:
        avg_speed = sim_car.speed_accum / sim_car.speed_samples
    outcome = CarRaceOutcome(
        race_time_seconds=sim_car.race_elapsed,
        best_lap_seconds=sim_car.best_lap_seconds,
        avg_speed=avg_speed,
        damage_taken=sim_car.state.damage,
        barrier_hits=sim_car.barrier_hits,
        completed=sim_car.state.laps > 0 and sim_car.state.state != "crashed",
    )
    sim_car.memory.remember(outcome)
    sim_car.learning.adapt(sim_car.behavior, sim_car.memory)


def main() -> int:
    project_dir = Path(__file__).resolve().parents[2]
    conf = read_simple_conf(
        project_dir / "etc" / "tracksim.conf",
        {
            "window_width": "1600",
            "window_height": "900",
            "tracks_dir": "tracks",
            "cars_dir": "cars",
        },
    )

    width = as_int(conf, "window_width", 1600)
    height = as_int(conf, "window_height", 900)
    tracks_dir = project_dir / conf.get("tracks_dir", "tracks")
    cars_dir = project_dir / conf.get("cars_dir", "cars")

    pygame.init()
    screen = pygame.display.set_mode((width, height))
    pygame.display.set_caption("Track Simulation")
    clock = pygame.time.Clock()
    font = create_default_font(22)
    status_font = create_default_font(16)
    crash_overlay = _load_crash_overlay(project_dir)

    track = None
    current_track_path: Path | None = None
    sim_cars: list[SimCar] = []
    selected_car_index: int | None = 0
    racing = False
    race_outcome_saved = True

    message = "L load track, C load car, N start/reset race, H toggle stats, arrows steer selected car, Q quit"
    open_menu: str | None = None
    header_rects: dict[str, pygame.Rect] = {}
    item_rects: dict[tuple[str, str], pygame.Rect] = {}
    load_picker_open = False
    load_picker_kind = "track"
    load_picker_files: list[Path] = []
    load_picker_rows: list[tuple[int, pygame.Rect]] = []

    up_pressed = False
    down_pressed = False
    left_pressed = False
    right_pressed = False
    autonomous_enabled = True
    show_stats = True

    dragging_index: int | None = None
    drag_offset = (0.0, 0.0)
    stats_dropdown = StatsDropdownState()

    menus = [
        ("Start", ["Load Track", "Load Car", "Save Track", "Quit"]),
        ("Race", ["Start Race", "Pause/Resume", "Reset Cars", "Quit Race"]),
    ]

    def ensure_selected_index() -> None:
        nonlocal selected_car_index
        if not sim_cars:
            selected_car_index = None
            stats_dropdown.open = False
            return
        if selected_car_index is None:
            return
        selected_car_index = max(0, min(selected_car_index, len(sim_cars) - 1))

    def add_loaded_car(car_path: Path, instance_name: str | None = None, start_pose: tuple[float, float, float] | None = None) -> None:
        nonlocal selected_car_index
        if track is None:
            return
        loaded = load_car(car_path)
        name = _make_unique_car_name(instance_name or loaded.name, sim_cars)
        profile, learning, pass_side_bias, pace_bias, steer_bias = _build_personality(name, loaded)
        state = spawn_state(track, loaded)
        pose = (state.x, state.y, state.heading_radians)
        if start_pose is not None:
            x, y, heading = start_pose
            if _is_on_surface(track, x, y):
                state.x = x
                state.y = y
                state.heading_radians = heading
                pose = (x, y, heading)
        sim_cars.append(
            SimCar(
                instance_name=name,
                source_file=car_path.name,
                config=loaded,
                state=state,
                start_pose=pose,
                behavior=profile,
                learning=learning,
                pass_side_bias=pass_side_bias,
                pace_bias=pace_bias,
                steer_bias=steer_bias,
            )
        )
        inserted_index = len(sim_cars) - 1
        open_pose = _find_open_pose(track, sim_cars, inserted_index, loaded, pose)
        sim_cars[inserted_index].state.x = open_pose[0]
        sim_cars[inserted_index].state.y = open_pose[1]
        sim_cars[inserted_index].state.heading_radians = open_pose[2]
        sim_cars[inserted_index].start_pose = open_pose
        selected_car_index = len(sim_cars) - 1

    running = True
    while running:
        dt = min(clock.get_time() / 1000.0, 0.05)

        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False
            elif event.type == pygame.KEYDOWN:
                if event.key == pygame.K_UP:
                    up_pressed = True
                elif event.key == pygame.K_DOWN:
                    down_pressed = True
                elif event.key == pygame.K_LEFT:
                    left_pressed = True
                elif event.key == pygame.K_RIGHT:
                    right_pressed = True

                if event.key == pygame.K_q:
                    running = False
                elif event.key == pygame.K_ESCAPE:
                    load_picker_open = False
                    open_menu = None
                    stats_dropdown.open = False
                elif event.key == pygame.K_l:
                    load_picker_kind = "track"
                    load_picker_files = sorted(tracks_dir.glob("*.track"))
                    load_picker_open = True
                    message = "Select a track file to load."
                elif event.key == pygame.K_c:
                    if track is None:
                        message = "Load a track before loading cars."
                    else:
                        load_picker_kind = "car"
                        load_picker_files = sorted(cars_dir.glob("*.car"))
                        load_picker_open = True
                        message = "Select a car file to load."
                elif event.key == pygame.K_n:
                    if track is None:
                        message = "Load a track before starting a race."
                    elif not sim_cars:
                        message = "Load at least one car first."
                    else:
                        for entry in sim_cars:
                            _reset_for_race(entry)
                        racing = True
                        race_outcome_saved = False
                        message = "Race started for all loaded cars."
                elif event.key == pygame.K_a:
                    autonomous_enabled = not autonomous_enabled
                    message = "Autonomous mode on." if autonomous_enabled else "Manual mode on."
                elif event.key == pygame.K_h:
                    show_stats = not show_stats
                    message = "Stats panel shown." if show_stats else "Stats panel hidden."
            elif event.type == pygame.KEYUP:
                if event.key == pygame.K_UP:
                    up_pressed = False
                elif event.key == pygame.K_DOWN:
                    down_pressed = False
                elif event.key == pygame.K_LEFT:
                    left_pressed = False
                elif event.key == pygame.K_RIGHT:
                    right_pressed = False
            elif event.type == pygame.MOUSEMOTION:
                if dragging_index is not None and not racing and track is not None:
                    entry = sim_cars[dragging_index]
                    nx = event.pos[0] + drag_offset[0]
                    ny = event.pos[1] + drag_offset[1]
                    if _is_on_surface(track, nx, ny) and not _car_overlaps_any(sim_cars, dragging_index, nx, ny, entry.config):
                        entry.state.x = nx
                        entry.state.y = ny
                        entry.start_pose = (entry.state.x, entry.state.y, entry.state.heading_radians)
            elif event.type == pygame.MOUSEBUTTONUP and event.button == 1:
                dragging_index = None
            elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
                if load_picker_open:
                    chosen_index = None
                    for idx, rect in load_picker_rows:
                        if rect.collidepoint(event.pos):
                            chosen_index = idx
                            break
                    if chosen_index is not None and chosen_index < len(load_picker_files):
                        chosen = load_picker_files[chosen_index]
                        if load_picker_kind == "car":
                            add_loaded_car(chosen)
                            message = f"Loaded car {sim_cars[-1].instance_name}."
                        else:
                            loaded_track = load_track(chosen)
                            track = loaded_track
                            current_track_path = chosen
                            sim_cars = []
                            selected_car_index = None

                            starts = loaded_track.metadata.get("car_starts", []) if isinstance(loaded_track.metadata, dict) else []
                            for raw in starts:
                                if not isinstance(raw, dict):
                                    continue
                                car_file = str(raw.get("car_file", "")).strip()
                                if not car_file:
                                    continue
                                pose_raw = raw.get("start_pose", [])
                                pose = None
                                if isinstance(pose_raw, (list, tuple)) and len(pose_raw) >= 3:
                                    pose = (float(pose_raw[0]), float(pose_raw[1]), float(pose_raw[2]))
                                instance_name = str(raw.get("instance_name", "")).strip() or None
                                car_path = cars_dir / car_file
                                if car_path.exists():
                                    add_loaded_car(car_path, instance_name=instance_name, start_pose=pose)

                            if not sim_cars:
                                latest_car = load_latest(cars_dir, ".car")
                                if latest_car is not None:
                                    add_loaded_car(latest_car)
                            message = f"Loaded track {chosen.name}."
                    load_picker_open = False
                    continue

                if stats_dropdown.open:
                    clicked_dropdown = False
                    for idx, rect in stats_dropdown.row_rects:
                        if rect.collidepoint(event.pos):
                            selected_car_index = idx
                            stats_dropdown.open = False
                            clicked_dropdown = True
                            break
                    if clicked_dropdown:
                        continue
                    if not stats_dropdown.header_rect.collidepoint(event.pos):
                        stats_dropdown.open = False

                if stats_dropdown.header_rect.collidepoint(event.pos) and show_stats and sim_cars:
                    stats_dropdown.open = not stats_dropdown.open
                    open_menu = None
                    continue

                if not racing and track is not None and sim_cars:
                    clicked_car = False
                    for idx in range(len(sim_cars) - 1, -1, -1):
                        entry = sim_cars[idx]
                        car_rect = _car_draw_rect(entry.state, entry.config)
                        if car_rect.collidepoint(event.pos):
                            dragging_index = idx
                            selected_car_index = idx
                            drag_offset = (entry.state.x - event.pos[0], entry.state.y - event.pos[1])
                            open_menu = None
                            clicked_car = True
                            break
                    if dragging_index is not None:
                        continue
                    if not clicked_car:
                        selected_car_index = None

                action = menu_action_at(event.pos, header_rects, item_rects)
                if action is None:
                    open_menu = None
                elif action.item == "":
                    open_menu = None if open_menu == action.menu else action.menu
                else:
                    open_menu = None
                    if action.menu == "Start" and action.item == "Quit":
                        running = False
                    elif action.menu == "Start" and action.item == "Load Track":
                        load_picker_kind = "track"
                        load_picker_files = sorted(tracks_dir.glob("*.track"))
                        load_picker_open = True
                        message = "Select a track file to load."
                    elif action.menu == "Start" and action.item == "Load Car":
                        if track is None:
                            message = "Load a track before loading cars."
                        else:
                            load_picker_kind = "car"
                            load_picker_files = sorted(cars_dir.glob("*.car"))
                            load_picker_open = True
                            message = "Select a car file to load."
                    elif action.menu == "Start" and action.item == "Save Track":
                        if track is None:
                            message = "Load a track before saving."
                        else:
                            if not isinstance(track.metadata, dict):
                                track.metadata = {}
                            track.metadata["car_starts"] = _serialize_car_starts(sim_cars)
                            if current_track_path is None:
                                safe_name = track.name.strip().replace(" ", "_") or "track"
                                current_track_path = tracks_dir / f"{safe_name}.track"
                            save_track(current_track_path, track)
                            message = f"Saved track {current_track_path.name} with {len(sim_cars)} cars."
                    elif action.menu == "Race" and action.item == "Start Race":
                        if track is None:
                            message = "Load a track before starting a race."
                        elif not sim_cars:
                            message = "Load at least one car first."
                        else:
                            for entry in sim_cars:
                                _reset_for_race(entry)
                            racing = True
                            race_outcome_saved = False
                            message = "Race started for all loaded cars."
                    elif action.menu == "Race" and action.item == "Pause/Resume":
                        if not sim_cars:
                            message = "Load at least one car first."
                        else:
                            racing = not racing
                            message = "Race resumed." if racing else "Race paused."
                    elif action.menu == "Race" and action.item == "Reset Cars":
                        if not sim_cars:
                            message = "No cars loaded."
                        else:
                            for entry in sim_cars:
                                _reset_for_race(entry)
                            racing = False
                            race_outcome_saved = True
                            message = "All cars reset to saved starting positions."
                    elif action.menu == "Race" and action.item == "Quit Race":
                        racing = False
                        race_outcome_saved = False
                        message = "Race ended."

        ensure_selected_index()

        screen.fill((42, 145, 75))
        if track is not None:
            draw_track(screen, track)

        if racing and track is not None and sim_cars:
            all_stopped = True
            for idx, entry in enumerate(sim_cars):
                state = entry.state
                car = entry.config
                if state.state != "crashed":
                    all_stopped = False

                traffic = [
                    (other.state.x, other.state.y, _car_collision_radius(other.config), other.state.speed)
                    for j, other in enumerate(sim_cars)
                    if j != idx and other.state.state != "crashed"
                ]

                if autonomous_enabled:
                    throttle, brake, steering = autonomous_controls(
                        state,
                        car,
                        track,
                        entry.behavior,
                        entry.learning,
                        traffic,
                        entry.pass_side_bias,
                        entry.pace_bias,
                        entry.steer_bias,
                    )
                else:
                    throttle = 0.0
                    brake = 0.0
                    steering = 0.0

                if selected_car_index is not None and idx == selected_car_index and (left_pressed or right_pressed or up_pressed or down_pressed):
                    throttle = 1.0 if up_pressed else 0.0
                    brake = 1.0 if down_pressed else 0.0
                    steering = 0.0
                    if left_pressed:
                        steering -= 1.0
                    if right_pressed:
                        steering += 1.0

                prev_laps = state.laps
                prev_wall_contact = state.wall_contact_frames

                update_car_state(state, car, track, dt, throttle, brake, steering)
                update_lap_counter(state, track)

                if state.wall_contact_frames > 0 and prev_wall_contact == 0:
                    entry.barrier_hits += 1

                if state.laps > prev_laps:
                    prev_best_lap = entry.best_lap_seconds
                    lap_time = entry.race_elapsed - entry.lap_start_time
                    if lap_time > 0.0:
                        entry.last_lap_seconds = lap_time
                    if lap_time > 0.0 and (entry.best_lap_seconds <= 0.0 or lap_time < entry.best_lap_seconds):
                        entry.best_lap_seconds = lap_time
                    entry.lap_start_time = entry.race_elapsed

                    lap_damage = max(0.0, state.damage - entry.last_lap_damage_checkpoint)
                    entry.last_lap_damage_checkpoint = state.damage

                    if lap_time > 0.0 and prev_best_lap > 0.0:
                        if lap_time < prev_best_lap * 0.985:
                            entry.learning.target_speed_bias = min(1.45, entry.learning.target_speed_bias + 0.03)
                            entry.learning.steering_aggression = min(1.35, entry.learning.steering_aggression + 0.015)
                            entry.learning.safety_bias = max(0.72, entry.learning.safety_bias - 0.01)
                        elif lap_time > prev_best_lap * 1.015:
                            entry.learning.target_speed_bias = max(0.72, entry.learning.target_speed_bias - 0.02)
                            entry.learning.safety_bias = min(1.6, entry.learning.safety_bias + 0.02)

                    if lap_damage > 6.0:
                        entry.learning.safety_bias = min(1.6, entry.learning.safety_bias + 0.05)
                        entry.learning.target_speed_bias = max(0.72, entry.learning.target_speed_bias - 0.03)
                        entry.learning.steering_aggression = max(0.75, entry.learning.steering_aggression - 0.02)

                entry.race_elapsed += dt
                entry.speed_accum += max(0.0, state.speed)
                entry.speed_samples += 1

            _resolve_car_overlaps(sim_cars)

            if all_stopped:
                racing = False
                message = "All cars are crashed/stopped. Press N to restart."

        if not racing and not race_outcome_saved and sim_cars:
            for entry in sim_cars:
                _finalize_race_outcome(entry)
            race_outcome_saved = True

        for idx, entry in enumerate(sim_cars):
            draw_car(screen, entry.state, entry.config)
            if entry.state.state == "crashed":
                if crash_overlay is not None:
                    overlay = pygame.transform.smoothscale(
                        crash_overlay,
                        (int(entry.config.length * 1.6), int(entry.config.width * 2.0)),
                    )
                    overlay = pygame.transform.rotate(overlay, -math.degrees(entry.state.heading_radians))
                    overlay_rect = overlay.get_rect(center=(entry.state.x, entry.state.y))
                    screen.blit(overlay, overlay_rect)
                else:
                    draw_crash_fallback(screen, entry.state, entry.config)

            if selected_car_index is not None and idx == selected_car_index:
                select_rect = _car_draw_rect(entry.state, entry.config).inflate(8, 8)
                pygame.draw.rect(screen, (255, 220, 120), select_rect, width=2)

        if show_stats and sim_cars:
            stats_index = selected_car_index if selected_car_index is not None else 0
            selected = sim_cars[stats_index]
            panel_rect = pygame.Rect(width - 296, height - 212, 284, 200)
            panel = pygame.Surface((panel_rect.width, panel_rect.height), pygame.SRCALPHA)
            panel.fill((24, 28, 32, 188))
            screen.blit(panel, panel_rect.topleft)

            stats_dropdown.header_rect = pygame.Rect(panel_rect.x + 12, panel_rect.y + 10, panel_rect.w - 24, 26)
            pygame.draw.rect(screen, (54, 62, 74), stats_dropdown.header_rect, border_radius=3)
            draw_lines(screen, status_font, [f"Car: {selected.instance_name}"], stats_dropdown.header_rect.x + 8, stats_dropdown.header_rect.y + 4, (235, 235, 235))

            stats_dropdown.row_rects = []
            if stats_dropdown.open:
                y = stats_dropdown.header_rect.bottom + 2
                for idx, entry in enumerate(sim_cars):
                    row = pygame.Rect(stats_dropdown.header_rect.x, y, stats_dropdown.header_rect.w, 24)
                    hovered = row.collidepoint(pygame.mouse.get_pos())
                    pygame.draw.rect(screen, (66, 76, 90) if hovered else (46, 54, 66), row, border_radius=2)
                    draw_lines(screen, status_font, [entry.instance_name], row.x + 6, row.y + 2, (235, 235, 235))
                    stats_dropdown.row_rects.append((idx, row))
                    y += 24

            compact_lines = [
                f"State: {selected.state.state}",
                f"Speed: {selected.state.speed:.1f}",
                f"Fuel: {selected.state.fuel:.1f}",
                f"Tire: {selected.state.tire_health:.1f}",
                f"Damage: {selected.state.damage:.1f}",
                f"Laps: {selected.state.laps}",
                f"Best Lap: {selected.best_lap_seconds:.2f}s" if selected.best_lap_seconds > 0 else "Best Lap: --",
                f"Memories: {len(selected.memory.recent_outcomes)}/10",
            ]
            draw_lines(screen, status_font, compact_lines, panel_rect.x + 12, panel_rect.y + 44, (235, 235, 235))

            debug_rect = pygame.Rect(panel_rect.x - 272, panel_rect.y, 260, 200)
            debug_panel = pygame.Surface((debug_rect.width, debug_rect.height), pygame.SRCALPHA)
            debug_panel.fill((20, 24, 28, 188))
            screen.blit(debug_panel, debug_rect.topleft)

            pygame.draw.rect(screen, (72, 86, 102), debug_rect, width=1)
            draw_lines(screen, status_font, ["Debug"], debug_rect.x + 10, debug_rect.y + 10, (200, 225, 255))

            last_lap_line = f"Last Lap: {selected.last_lap_seconds:.2f}s" if selected.last_lap_seconds > 0 else "Last Lap: --"
            dbg_lines = [
                last_lap_line,
                f"Lap Start: {selected.lap_start_time:.2f}s",
                f"Race T: {selected.race_elapsed:.2f}s",
                f"Dist: {selected.state.distance_traveled:.1f}",
                f"Hits: {selected.barrier_hits}",
                f"v: ({selected.state.vx:.1f}, {selected.state.vy:.1f})",
                f"yaw_rate: {selected.state.yaw_rate:.2f}",
                f"spd_bias: {selected.learning.target_speed_bias:.2f}",
                f"steer_aggr: {selected.learning.steering_aggression:.2f}",
                f"safe_bias: {selected.learning.safety_bias:.2f}",
            ]
            draw_lines(screen, status_font, dbg_lines, debug_rect.x + 10, debug_rect.y + 34, (225, 225, 225))

        mode_line = "AUTO (A)" if autonomous_enabled else "MANUAL (A)"
        mode_color = (180, 210, 255) if autonomous_enabled else (240, 210, 170)
        draw_lines(screen, font, [mode_line], width - 178, 46, mode_color)

        draw_lines(screen, font, [message], 24, height - 40, (245, 245, 245))

        if load_picker_open:
            picker_title = "Load Car" if load_picker_kind == "car" else "Load Track"
            _, load_picker_rows = draw_file_picker(
                screen,
                font,
                picker_title,
                [p.name for p in load_picker_files],
                pygame.mouse.get_pos(),
            )

        header_rects, item_rects = draw_dropdown_menus(
            screen,
            font,
            menus,
            open_menu,
            pygame.mouse.get_pos(),
        )
        pygame.display.flip()
        clock.tick(60)

    pygame.quit()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
