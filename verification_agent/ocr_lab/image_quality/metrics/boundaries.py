"""Document boundary, cropping, in-plane rotation, and perspective metrics."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import cv2
import numpy as np

from ..normalization import WorkingImage
from ..output_schema import MetricResult


@dataclass(frozen=True)
class BoundaryResult:
    valid: bool
    confidence: float
    area_ratio: float
    bbox: tuple[int, int, int, int] | None
    quad: np.ndarray | None
    border_sides_touching: int
    cropping_suspected: bool
    cropping_confirmed: bool
    aspect_ratio: float | None
    aspect_ratio_in_range: bool | None
    edge_density: float
    frame_border_ambiguous: bool
    no_document_signals: int
    candidate_count: int
    solidity: float
    method: str = "contour_quadrilateral"

    def to_metric(self, cfg: dict[str, Any]) -> MetricResult:
        value = {
            "valid": self.valid,
            "area_ratio": round(float(self.area_ratio), 4),
            "bounding_box_working": list(self.bbox) if self.bbox is not None else None,
            "quad_working": (
                [[round(float(x), 2), round(float(y), 2)] for x, y in self.quad]
                if self.quad is not None
                else None
            ),
            "border_sides_touching": int(self.border_sides_touching),
            "cropping_suspected": bool(self.cropping_suspected),
            "cropping_confirmed": bool(self.cropping_confirmed),
            "aspect_ratio": (
                round(float(self.aspect_ratio), 4) if self.aspect_ratio is not None else None
            ),
            "aspect_ratio_in_range": self.aspect_ratio_in_range,
            "edge_density": round(float(self.edge_density), 5),
            "frame_border_ambiguous": bool(self.frame_border_ambiguous),
            "no_document_signals": int(self.no_document_signals),
            "candidate_count": int(self.candidate_count),
            "solidity": round(float(self.solidity), 4),
        }
        if self.valid:
            nature = "heuristic"
            confidence = min(0.95, max(0.0, float(self.confidence)))
        elif self.frame_border_ambiguous:
            nature = "heuristic"
            confidence = 0.3
        else:
            nature = "unknown"
            confidence = 0.0
        return MetricResult(
            value=value,
            nature=nature,
            confidence=confidence,
            metadata={
                "detector": self.method,
                "min_confidence": cfg["min_confidence"],
                "min_candidate_area_ratio": cfg["min_candidate_area_ratio"],
                "aspect_ratio_range": cfg["aspect_ratio_range"],
                "cropping_min_sides": cfg["cropping_min_sides"],
                "cropping_min_confidence": cfg["cropping_min_confidence"],
                "note": "A rectangle is not proof of a document; boundary detection is heuristic.",
            },
        )


def _order_quad(points: np.ndarray) -> np.ndarray:
    points = np.asarray(points, dtype=np.float32).reshape(4, 2)
    sums = points.sum(axis=1)
    diffs = np.diff(points, axis=1).ravel()
    top_left = points[np.argmin(sums)]
    bottom_right = points[np.argmax(sums)]
    top_right = points[np.argmin(diffs)]
    bottom_left = points[np.argmax(diffs)]
    return np.array([top_left, top_right, bottom_right, bottom_left], dtype=np.float32)


def _side_lengths(quad: np.ndarray) -> tuple[float, float, float, float]:
    top = float(np.linalg.norm(quad[1] - quad[0]))
    right = float(np.linalg.norm(quad[2] - quad[1]))
    bottom = float(np.linalg.norm(quad[3] - quad[2]))
    left = float(np.linalg.norm(quad[0] - quad[3]))
    return top, right, bottom, left


def _touches_border(quad: np.ndarray, width: int, height: int, tolerance: float) -> int:
    sides = 0
    for index in range(4):
        first = quad[index]
        second = quad[(index + 1) % 4]
        if abs(float(first[0])) <= tolerance and abs(float(second[0])) <= tolerance:
            sides += 1
        elif (
            abs(float(first[0]) - (width - 1)) <= tolerance
            and abs(float(second[0]) - (width - 1)) <= tolerance
        ):
            sides += 1
        elif abs(float(first[1])) <= tolerance and abs(float(second[1])) <= tolerance:
            sides += 1
        elif (
            abs(float(first[1]) - (height - 1)) <= tolerance
            and abs(float(second[1]) - (height - 1)) <= tolerance
        ):
            sides += 1
    return sides


def _central_edge_density(
    gray: np.ndarray,
    central_fraction: float,
    canny_low: int,
    canny_high: int,
) -> float:
    height, width = gray.shape[:2]
    fraction = float(np.clip(central_fraction, 0.1, 1.0))
    crop_width = max(8, int(round(width * fraction)))
    crop_height = max(8, int(round(height * fraction)))
    x0 = max(0, (width - crop_width) // 2)
    y0 = max(0, (height - crop_height) // 2)
    crop = gray[y0 : y0 + crop_height, x0 : x0 + crop_width]
    edges = cv2.Canny(crop, int(canny_low), int(canny_high))
    return float(np.count_nonzero(edges)) / float(crop.size)


def _empty_boundary(edge_density: float, candidate_count: int, cfg: dict[str, Any]) -> BoundaryResult:
    signals = 0
    signals += 1  # no reliable quadrilateral
    signals += 1  # no usable document-region area
    if edge_density < float(cfg["no_document_min_edge_density"]):
        signals += 1
    if candidate_count == 0:
        signals += 1
    return BoundaryResult(
        valid=False,
        confidence=0.0,
        area_ratio=0.0,
        bbox=None,
        quad=None,
        border_sides_touching=0,
        cropping_suspected=False,
        cropping_confirmed=False,
        aspect_ratio=None,
        aspect_ratio_in_range=None,
        edge_density=edge_density,
        frame_border_ambiguous=False,
        no_document_signals=signals,
        candidate_count=candidate_count,
        solidity=0.0,
        method="no_candidate",
    )


def _bright_region_candidate(
    blurred: np.ndarray,
    *,
    width: int,
    height: int,
    edge_density: float,
    cfg: dict[str, Any],
    tolerance: float,
) -> dict[str, Any] | None:
    """Fallback for blurred or border-touching bright document regions.

    Otsu thresholding is used only as a geometric fallback.  It can propose a
    large, solid region, but it is never treated as a document without the
    edge-density and border checks below.
    """

    _threshold, mask = cv2.threshold(
        blurred,
        0,
        255,
        cv2.THRESH_BINARY + cv2.THRESH_OTSU,
    )
    contours, _hierarchy = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    total_area = float(width * height)
    minimum_area = float(cfg["fallback_min_area_ratio"]) * total_area
    best: dict[str, Any] | None = None
    best_score = -1.0

    for contour in contours:
        area = float(cv2.contourArea(contour))
        if area < minimum_area:
            continue
        hull = cv2.convexHull(contour)
        hull_area = float(cv2.contourArea(hull))
        if hull_area <= 0:
            continue
        solidity = area / hull_area
        if solidity < float(cfg["min_solidity"]):
            continue

        perimeter = float(cv2.arcLength(contour, True))
        approximation = cv2.approxPolyDP(
            contour,
            float(cfg["approx_epsilon_fraction"]) * perimeter,
            True,
        )
        method = "otsu_bright_region_quadrilateral"
        if len(approximation) == 4 and cv2.isContourConvex(approximation):
            quad = _order_quad(approximation.reshape(4, 2))
        else:
            box = cv2.boxPoints(cv2.minAreaRect(contour))
            quad = _order_quad(box)
            method = "otsu_bright_region_min_area_rect"
        if len(np.unique(quad, axis=0)) < 4:
            continue

        area_ratio = area / total_area
        border_sides = _touches_border(quad, width, height, tolerance)
        if border_sides >= 2 and edge_density < float(cfg["no_document_min_edge_density"]):
            # A smooth gradient can split into a large region touching the frame.
            continue
        if area_ratio >= 0.985 and border_sides >= 4 and edge_density < float(
            cfg["no_document_min_edge_density"]
        ):
            continue

        score = area_ratio * solidity
        if score > best_score:
            best_score = score
            confidence = min(
                0.9,
                float(cfg["fallback_confidence"]) + 0.15 * min(1.0, area_ratio),
            )
            best = {
                "quad": quad,
                "area_ratio": area_ratio,
                "solidity": float(solidity),
                "bbox": cv2.boundingRect(quad.astype(np.int32)),
                "border_sides": border_sides,
                "confidence": confidence,
                "method": method,
            }
    return best


def detect_document_boundary(working: WorkingImage, cfg: dict[str, Any]) -> BoundaryResult:
    """Detect the most plausible document quadrilateral.

    This deliberately does not use OCR, text orientation, or any semantic
    document model.  It only proposes geometric candidates and reports its
    confidence.  Rounded corners, plastic sleeves, and UI overlays can produce
    false positives, which is why the result is marked as heuristic.
    """

    image = working.array
    height, width = image.shape[:2]
    gray = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY)

    if min(width, height) < int(cfg.get("min_analysis_side_px", 64)):
        return BoundaryResult(
            valid=False,
            confidence=0.0,
            area_ratio=0.0,
            bbox=None,
            quad=None,
            border_sides_touching=0,
            cropping_suspected=False,
            cropping_confirmed=False,
            aspect_ratio=None,
            aspect_ratio_in_range=None,
            edge_density=0.0,
            frame_border_ambiguous=False,
            no_document_signals=0,
            candidate_count=0,
            solidity=0.0,
            method="too_small_for_boundary",
        )

    edge_density = _central_edge_density(
        gray,
        central_fraction=float(cfg.get("central_fraction", 0.8)),
        canny_low=int(cfg["canny_low"]),
        canny_high=int(cfg["canny_high"]),
    )

    blurred = cv2.GaussianBlur(gray, (5, 5), 0)
    edges = cv2.Canny(blurred, int(cfg["canny_low"]), int(cfg["canny_high"]))
    median = float(np.median(blurred))
    adaptive_low = int(max(0, min(255, 0.66 * median)))
    adaptive_high = int(max(adaptive_low + 30, min(255, 1.33 * median)))
    edges = cv2.bitwise_or(edges, cv2.Canny(blurred, adaptive_low, adaptive_high))
    kernel = np.ones((3, 3), np.uint8)
    edges = cv2.dilate(edges, kernel, iterations=int(cfg["dilate_iterations"]))

    contours, _hierarchy = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    total_area = float(width * height)
    min_candidate_area = float(cfg["min_candidate_area_ratio"]) * total_area
    tolerance = float(cfg["border_tolerance_fraction"]) * max(width, height)

    candidate_count = 0
    best: dict[str, Any] | None = None
    best_score = -1.0

    for contour in contours:
        area = float(cv2.contourArea(contour))
        if area < min_candidate_area:
            continue
        hull = cv2.convexHull(contour)
        hull_area = float(cv2.contourArea(hull))
        if hull_area <= 0:
            continue
        solidity = area / hull_area
        if solidity < float(cfg["min_solidity"]):
            continue

        perimeter = float(cv2.arcLength(contour, True))
        approximation = cv2.approxPolyDP(
            contour,
            float(cfg["approx_epsilon_fraction"]) * perimeter,
            True,
        )
        if len(approximation) != 4 or not cv2.isContourConvex(approximation):
            continue

        quad = _order_quad(approximation.reshape(4, 2))
        if len(np.unique(quad, axis=0)) < 4:
            continue

        candidate_count += 1
        area_ratio = area / total_area
        score = area_ratio * float(solidity)
        if score > best_score:
            best_score = score
            best = {
                "quad": quad,
                "area_ratio": area_ratio,
                "solidity": float(solidity),
                "bbox": cv2.boundingRect(quad.astype(np.int32)),
                "border_sides": _touches_border(quad, width, height, tolerance),
                "method": "contour_quadrilateral",
            }

    bright_candidate = _bright_region_candidate(
        blurred,
        width=width,
        height=height,
        edge_density=edge_density,
        cfg=cfg,
        tolerance=tolerance,
    )
    if bright_candidate is not None and (
        best is None or float(bright_candidate["area_ratio"]) > float(best["area_ratio"]) * 1.2
    ):
        best = bright_candidate

    if best is None:
        return _empty_boundary(edge_density, candidate_count, cfg)

    quad = best["quad"]
    area_ratio = float(best["area_ratio"])
    bbox = tuple(int(value) for value in best["bbox"])
    border_sides = int(best["border_sides"])
    solidity = float(best["solidity"])
    method = str(best.get("method", "contour_quadrilateral"))
    confidence = float(
        best.get(
            "confidence",
            min(0.95, 0.40 + 0.35 * min(1.0, area_ratio) + 0.20 * solidity),
        )
    )

    is_frame_rectangle = (
        area_ratio >= 0.985
        and border_sides >= 4
        and bbox[0] <= 2
        and bbox[1] <= 2
        and bbox[2] >= width - 3
        and bbox[3] >= height - 3
    )
    if is_frame_rectangle and edge_density < float(cfg["no_document_min_edge_density"]):
        return BoundaryResult(
            valid=False,
            confidence=0.0,
            area_ratio=1.0,
            bbox=bbox,
            quad=quad,
            border_sides_touching=border_sides,
            cropping_suspected=True,
            cropping_confirmed=False,
            aspect_ratio=None,
            aspect_ratio_in_range=None,
            edge_density=edge_density,
            frame_border_ambiguous=True,
            no_document_signals=2,
            candidate_count=candidate_count,
            solidity=solidity,
            method=method,
        )

    valid = confidence >= float(cfg["min_confidence"])
    top, right, bottom, left = _side_lengths(quad)
    document_width = max(1e-6, (top + bottom) / 2.0)
    document_height = max(1e-6, (left + right) / 2.0)
    aspect_ratio = max(document_width, document_height) / min(document_width, document_height)
    aspect_range = cfg.get("aspect_ratio_range")
    if aspect_range is None:
        aspect_in_range: bool | None = None
    else:
        low, high = float(aspect_range[0]), float(aspect_range[1])
        aspect_in_range = low <= aspect_ratio <= high

    cropping_suspected = border_sides >= int(cfg["cropping_min_sides"])
    cropping_confirmed = (
        cropping_suspected
        and valid
        and confidence >= float(cfg["cropping_min_confidence"])
    )

    return BoundaryResult(
        valid=valid,
        confidence=confidence,
        area_ratio=area_ratio,
        bbox=bbox,
        quad=quad,
        border_sides_touching=border_sides,
        cropping_suspected=cropping_suspected,
        cropping_confirmed=cropping_confirmed,
        aspect_ratio=aspect_ratio,
        aspect_ratio_in_range=aspect_in_range,
        edge_density=edge_density,
        frame_border_ambiguous=False,
        no_document_signals=0,
        candidate_count=candidate_count,
        solidity=solidity,
        method=method,
    )


def _normalise_angle(angle: float) -> float:
    value = float(angle)
    while value <= -45.0:
        value += 90.0
    while value > 45.0:
        value -= 90.0
    return value


def compute_rotation_metric(boundary: BoundaryResult, cfg: dict[str, Any]) -> MetricResult:
    value: dict[str, Any] = {
        "angle_deg": None,
        "orientation_90_180": "unknown",
    }
    if not boundary.valid or boundary.quad is None:
        return MetricResult(
            value=value,
            nature="unknown",
            confidence=0.0,
            metadata={
                "max_abs_angle_deg": cfg["max_abs_angle_deg"],
                "note": "90/180-degree orientation is not guessed in this phase.",
            },
        )

    quad = boundary.quad
    top, right, bottom, left = _side_lengths(quad)
    if (top + bottom) >= (left + right):
        vector = quad[1] - quad[0]
    else:
        vector = quad[3] - quad[0]
    angle = float(np.degrees(np.arctan2(float(vector[1]), float(vector[0]))))
    angle = _normalise_angle(angle)
    value["angle_deg"] = round(angle, 3)
    return MetricResult(
        value=value,
        nature="heuristic",
        confidence=min(0.9, max(0.0, boundary.confidence) * 0.9),
        metadata={
            "max_abs_angle_deg": cfg["max_abs_angle_deg"],
            "method": "longer_quad_edge_angle",
            "note": "90/180-degree orientation is not guessed in this phase.",
        },
    )


def compute_perspective_metric(boundary: BoundaryResult, cfg: dict[str, Any]) -> MetricResult:
    value: dict[str, Any] = {
        "ratio": None,
        "top_bottom_ratio": None,
        "left_right_ratio": None,
    }
    if not boundary.valid or boundary.quad is None:
        return MetricResult(
            value=value,
            nature="unknown",
            confidence=0.0,
            metadata={"max_perspective_ratio": cfg["max_perspective_ratio"]},
        )

    top, right, bottom, left = _side_lengths(boundary.quad)
    top_bottom = abs(top - bottom) / max(top, bottom, 1e-6)
    left_right = abs(left - right) / max(left, right, 1e-6)
    ratio = max(top_bottom, left_right)
    value.update(
        {
            "ratio": round(float(ratio), 4),
            "top_bottom_ratio": round(float(top_bottom), 4),
            "left_right_ratio": round(float(left_right), 4),
        }
    )
    return MetricResult(
        value=value,
        nature="heuristic",
        confidence=min(0.9, max(0.0, boundary.confidence) * 0.9),
        metadata={
            "max_perspective_ratio": cfg["max_perspective_ratio"],
            "method": "opposite_side_length_imbalance",
        },
    )
