"""Document boundary, cropping, in-plane rotation, and perspective metrics."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any

import cv2
import numpy as np

from ..normalization import WorkingImage
from ..output_schema import MetricResult

REJECTION_REASON_KEYS = (
    "aspect_ratio",
    "area_ratio",
    "solidity",
    "confidence",
    "border_contact",
    "none_found",
)


def _rejection_payload(counts: Counter[str] | dict[str, int] | None) -> dict[str, int]:
    source = counts or {}
    return {key: int(source.get(key, 0)) for key in REJECTION_REASON_KEYS}


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
    warning: str | None = None
    rejection_counts: dict[str, int] = field(default_factory=dict)
    clear_rectangle_heuristic: bool = False
    rectangle_edge_counts: dict[str, int] = field(default_factory=dict)

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
            "detector_used": self.method,
            "warning": self.warning,
            "rejection_counts": _rejection_payload(self.rejection_counts),
            "clear_rectangle_heuristic": bool(self.clear_rectangle_heuristic),
            "rectangle_edge_counts": {
                "horizontal": int(self.rectangle_edge_counts.get("horizontal", 0)),
                "vertical": int(self.rectangle_edge_counts.get("vertical", 0)),
            },
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


def _outside_edge_density(
    gray: np.ndarray,
    central_fraction: float,
    canny_low: int,
    canny_high: int,
) -> float:
    """Return Canny edge density in the area outside the central crop."""

    height, width = gray.shape[:2]
    fraction = float(np.clip(central_fraction, 0.1, 1.0))
    crop_width = max(8, int(round(width * fraction)))
    crop_height = max(8, int(round(height * fraction)))
    x0 = max(0, (width - crop_width) // 2)
    y0 = max(0, (height - crop_height) // 2)
    mask = np.ones((height, width), dtype=np.uint8)
    mask[y0 : y0 + crop_height, x0 : x0 + crop_width] = 0
    outside_area = int(np.count_nonzero(mask))
    if outside_area <= 0:
        return 0.0
    edges = cv2.Canny(gray, int(canny_low), int(canny_high))
    return float(np.count_nonzero(cv2.bitwise_and(edges, mask))) / float(outside_area)


def _border_content_sides(
    gray: np.ndarray,
    *,
    canny_low: int,
    canny_high: int,
    strip_fraction: float,
    minimum_density: float,
) -> int:
    """Count frame sides that contain visible high-contrast content.

    This is used only when no quadrilateral candidate exists.  A card that is
    cut off by the camera often has text or card texture running into two or
    more frame edges, even though there is no closed card outline.
    """

    height, width = gray.shape[:2]
    edges = cv2.Canny(gray, int(canny_low), int(canny_high))
    strip = max(2, int(round(min(width, height) * strip_fraction)))
    sides = 0
    for strip_edges in (
        edges[:strip, :],
        edges[height - strip :, :],
        edges[:, :strip],
        edges[:, width - strip :],
    ):
        if strip_edges.size == 0:
            continue
        density = float(np.count_nonzero(strip_edges)) / float(strip_edges.size)
        if density >= minimum_density:
            sides += 1
    return sides


def _cluster_axis_positions(positions: list[float], tolerance: float) -> int:
    """Count distinct parallel edge positions within a tolerance band."""

    if not positions:
        return 0
    ordered = sorted(positions)
    clusters = 1
    previous = ordered[0]
    for position in ordered[1:]:
        if position - previous > tolerance:
            clusters += 1
        previous = position
    return clusters


def _strong_rectangle_edges(
    gray: np.ndarray,
    cfg: dict[str, Any],
) -> tuple[bool, dict[str, int]]:
    """Return a cheap four-strong-edge heuristic for diagnostics only.

    This deliberately does not feed back into the decision engine.  It is
    useful when the closed-contour detector finds no candidate: two long
    near-horizontal and two long near-vertical Hough segments indicate that a
    human-visible rectangle exists even though contour closing failed.
    """

    height, width = gray.shape[:2]
    min_side = float(min(width, height))
    blurred = cv2.GaussianBlur(gray, (5, 5), 0)
    edges = cv2.Canny(
        blurred,
        int(cfg["canny_low"]),
        int(cfg["canny_high"]),
    )
    lines = cv2.HoughLinesP(
        edges,
        1,
        np.pi / 180.0,
        threshold=max(24, int(min_side * 0.06)),
        minLineLength=max(24, int(min_side * 0.20)),
        maxLineGap=max(8, int(min_side * 0.04)),
    )
    horizontal: list[float] = []
    vertical: list[float] = []
    if lines is not None:
        for line in lines:
            coordinates = np.asarray(line).reshape(-1)
            if coordinates.size < 4:
                continue
            x1, y1, x2, y2 = (float(value) for value in coordinates[:4])
            dx = abs(x2 - x1)
            dy = abs(y2 - y1)
            if np.hypot(dx, dy) < min_side * 0.20:
                continue
            angle = float(np.degrees(np.arctan2(dy, dx)))
            if angle <= 15.0:
                horizontal.append((y1 + y2) / 2.0)
            elif angle >= 75.0:
                vertical.append((x1 + x2) / 2.0)
    tolerance = max(8.0, min_side * 0.03)
    horizontal_count = _cluster_axis_positions(horizontal, tolerance)
    vertical_count = _cluster_axis_positions(vertical, tolerance)
    counts = {"horizontal": horizontal_count, "vertical": vertical_count}
    return horizontal_count >= 2 and vertical_count >= 2, counts


def _center_crop_candidate(
    *,
    width: int,
    height: int,
    edge_density: float,
    cfg: dict[str, Any],
) -> dict[str, Any]:
    """Assume the card fills the central crop when the outside is featureless."""

    fraction = float(np.clip(cfg.get("center_crop_fraction", 0.8), 0.2, 1.0))
    crop_width = max(8, min(width, int(round(width * fraction))))
    crop_height = max(8, min(height, int(round(height * fraction))))
    x0 = max(0, (width - crop_width) // 2)
    y0 = max(0, (height - crop_height) // 2)
    x1 = x0 + crop_width - 1
    y1 = y0 + crop_height - 1
    quad = np.array(
        [[x0, y0], [x1, y0], [x1, y1], [x0, y1]],
        dtype=np.float32,
    )
    return {
        "quad": quad,
        "area_ratio": (crop_width * crop_height) / float(width * height),
        "solidity": 1.0,
        "bbox": (x0, y0, crop_width, crop_height),
        "border_sides": 0,
        "confidence": 0.60,
        "method": "center_crop_fallback",
        "candidate_count": 0,
    }


def _full_frame_candidate(
    *,
    width: int,
    height: int,
    edge_density: float,
    best: dict[str, Any],
    warning: str | None = None,
) -> dict[str, Any]:
    """Use the whole frame when a detected card fills most of it."""

    x1 = width - 1
    y1 = height - 1
    quad = np.array(
        [[0, 0], [x1, 0], [x1, y1], [0, y1]],
        dtype=np.float32,
    )
    return {
        "quad": quad,
        "area_ratio": 1.0,
        "solidity": 1.0,
        "bbox": (0, 0, width, height),
        "border_sides": 0,
        "confidence": min(
            0.90,
            max(0.70, float(best.get("confidence", 0.70))),
        ),
        "method": "full_frame",
        "candidate_count": int(best.get("candidate_count", 0)),
        "edge_density": edge_density,
        "warning": warning,
    }


def _hough_rectangle_candidate(
    blurred: np.ndarray,
    *,
    width: int,
    height: int,
    cfg: dict[str, Any],
    tolerance: float,
    edge_density: float,
    rejection_counts: Counter[str],
) -> dict[str, Any] | None:
    """Recover a broken card rectangle from straight edge segments.

    Canny contours require a closed contour.  A dashed or shadow-broken card
    border can therefore fail even though its four sides are visibly straight.
    HoughLinesP bridges small gaps; drawing the resulting lines into a mask lets
    the normal quadrilateral and solidity checks run on a closed shape.
    """

    min_side = float(min(width, height))
    edges = cv2.Canny(
        blurred,
        int(cfg["canny_low"]),
        int(cfg["canny_high"]),
    )
    if np.count_nonzero(edges) < 16:
        return None

    lines = cv2.HoughLinesP(
        edges,
        1,
        np.pi / 180.0,
        threshold=max(24, int(min_side * 0.06)),
        minLineLength=max(20, int(min_side * 0.16)),
        maxLineGap=max(8, int(min_side * 0.035)),
    )
    if lines is None or len(lines) < 4:
        return None

    mask = np.zeros((height, width), dtype=np.uint8)
    min_line_length = max(20.0, min_side * 0.12)
    line_count = 0
    for line in lines:
        coordinates = np.asarray(line).reshape(-1)
        if coordinates.size < 4:
            continue
        x1, y1, x2, y2 = (int(value) for value in coordinates[:4])
        length = float(np.hypot(x2 - x1, y2 - y1))
        if length < min_line_length:
            continue
        cv2.line(mask, (x1, y1), (x2, y2), 255, 2)
        line_count += 1
    if line_count < 4 or np.count_nonzero(mask) < 16:
        return None

    mask = cv2.morphologyEx(
        mask,
        cv2.MORPH_CLOSE,
        np.ones((5, 5), np.uint8),
        iterations=1,
    )
    contours, _hierarchy = cv2.findContours(
        mask,
        cv2.RETR_EXTERNAL,
        cv2.CHAIN_APPROX_SIMPLE,
    )
    total_area = float(width * height)
    minimum_area = float(cfg["min_candidate_area_ratio"]) * total_area
    best: dict[str, Any] | None = None
    best_score = -1.0
    candidate_count = 0

    for contour in contours:
        area = float(cv2.contourArea(contour))
        if area < minimum_area:
            rejection_counts["area_ratio"] += 1
            continue
        hull = cv2.convexHull(contour)
        hull_area = float(cv2.contourArea(hull))
        if hull_area <= 0:
            rejection_counts["solidity"] += 1
            continue
        solidity = area / hull_area
        if solidity < float(cfg["min_solidity"]):
            rejection_counts["solidity"] += 1
            continue

        perimeter = float(cv2.arcLength(contour, True))
        approximation = cv2.approxPolyDP(
            contour,
            float(cfg["approx_epsilon_fraction"]) * perimeter,
            True,
        )
        area_ratio = area / total_area
        if len(approximation) == 4 and cv2.isContourConvex(approximation):
            quad = _order_quad(approximation.reshape(4, 2))
        elif area_ratio >= 0.25:
            box = cv2.boxPoints(cv2.minAreaRect(contour))
            quad = _order_quad(box)
        else:
            rejection_counts["area_ratio"] += 1
            continue
        if len(np.unique(quad, axis=0)) < 4:
            rejection_counts["area_ratio"] += 1
            continue

        candidate_count += 1
        score = area_ratio * solidity
        if score <= best_score:
            continue
        best_score = score
        best = {
            "quad": quad,
            "area_ratio": area_ratio,
            "solidity": float(solidity),
            "bbox": cv2.boundingRect(quad.astype(np.int32)),
            "border_sides": _touches_border(quad, width, height, tolerance),
            "confidence": min(
                0.90,
                0.55 + 0.30 * min(1.0, area_ratio) + 0.10 * solidity,
            ),
            "method": "hough_lines",
            "candidate_count": candidate_count,
        }

    if best is not None:
        best["candidate_count"] = candidate_count
        best["edge_density"] = edge_density
    return best


def _empty_boundary(
    edge_density: float,
    candidate_count: int,
    cfg: dict[str, Any],
    *,
    border_sides_touching: int = 0,
    rejection_counts: Counter[str] | dict[str, int] | None = None,
    clear_rectangle_heuristic: bool = False,
    rectangle_edge_counts: dict[str, int] | None = None,
) -> BoundaryResult:
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
        border_sides_touching=border_sides_touching,
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
        rejection_counts=_rejection_payload(rejection_counts),
        clear_rectangle_heuristic=bool(clear_rectangle_heuristic),
        rectangle_edge_counts=dict(rectangle_edge_counts or {}),
    )


def _bright_region_candidate(
    blurred: np.ndarray,
    *,
    width: int,
    height: int,
    edge_density: float,
    cfg: dict[str, Any],
    tolerance: float,
    rejection_counts: Counter[str],
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
            rejection_counts["area_ratio"] += 1
            continue
        hull = cv2.convexHull(contour)
        hull_area = float(cv2.contourArea(hull))
        if hull_area <= 0:
            rejection_counts["solidity"] += 1
            continue
        solidity = area / hull_area
        if solidity < float(cfg["min_solidity"]):
            rejection_counts["solidity"] += 1
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
            rejection_counts["area_ratio"] += 1
            continue

        area_ratio = area / total_area
        border_sides = _touches_border(quad, width, height, tolerance)
        if border_sides >= 2 and edge_density < float(cfg["no_document_min_edge_density"]):
            # A smooth gradient can split into a large region touching the frame.
            rejection_counts["border_contact"] += 1
            continue
        # A whole-frame Otsu region is usually the background, not the card.
        # Let the explicit full-frame fallback handle a card that really fills
        # the frame; otherwise the frame border would be misread as CROPPED.
        if area_ratio >= 0.985 and border_sides >= 4:
            rejection_counts["border_contact"] += 1
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


def _adaptive_threshold_rectangle_candidate(
    gray: np.ndarray,
    *,
    width: int,
    height: int,
    edge_density: float,
    cfg: dict[str, Any],
    tolerance: float,
    rejection_counts: Counter[str],
) -> dict[str, Any] | None:
    """Find a large, card-shaped region in an adaptive-threshold mask."""

    mask = cv2.adaptiveThreshold(
        gray,
        255,
        cv2.ADAPTIVE_THRESH_MEAN_C,
        cv2.THRESH_BINARY,
        17,
        1,
    )
    kernel = np.ones((3, 3), np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
    contours, _hierarchy = cv2.findContours(
        mask,
        cv2.RETR_EXTERNAL,
        cv2.CHAIN_APPROX_SIMPLE,
    )
    total_area = float(width * height)
    best: dict[str, Any] | None = None
    best_score = -1.0

    for contour in contours:
        contour_area = float(cv2.contourArea(contour))
        if contour_area <= 0.05 * total_area:
            rejection_counts["area_ratio"] += 1
            continue
        hull = cv2.convexHull(contour)
        hull_area = float(cv2.contourArea(hull))
        if hull_area <= 0.0:
            rejection_counts["solidity"] += 1
            continue
        solidity = contour_area / hull_area
        if solidity <= 0.85:
            rejection_counts["solidity"] += 1
            continue

        rectangle = cv2.minAreaRect(contour)
        (_center_x, _center_y), (rect_width, rect_height), _angle = rectangle
        if min(rect_width, rect_height) <= 0.0:
            rejection_counts["area_ratio"] += 1
            continue
        aspect_ratio = max(rect_width, rect_height) / min(rect_width, rect_height)
        if not 1.3 <= aspect_ratio <= 2.0:
            rejection_counts["aspect_ratio"] += 1
            continue

        quad = _order_quad(cv2.boxPoints(rectangle))
        if len(np.unique(quad, axis=0)) < 4:
            rejection_counts["area_ratio"] += 1
            continue
        rectangle_area = float(rect_width * rect_height)
        area_ratio = rectangle_area / total_area
        border_sides = _touches_border(quad, width, height, tolerance)
        if area_ratio >= 0.985 and border_sides >= 4:
            rejection_counts["border_contact"] += 1
            continue
        score = contour_area * solidity
        if score <= best_score:
            continue
        best_score = score
        best = {
            "quad": quad,
            "area_ratio": area_ratio,
            "solidity": float(solidity),
            "bbox": cv2.boundingRect(quad.astype(np.int32)),
            "border_sides": border_sides,
            "confidence": min(
                0.9,
                float(cfg["fallback_confidence"]) + 0.15 * min(1.0, area_ratio),
            ),
            "method": "adaptive_threshold_rectangle",
            "candidate_count": 1,
            "edge_density": edge_density,
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
            rejection_counts=_rejection_payload({"none_found": 1}),
        )

    central_fraction = float(cfg.get("central_fraction", 0.8))
    edge_density = _central_edge_density(
        gray,
        central_fraction=central_fraction,
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
    rejection_counts: Counter[str] = Counter()

    for contour in contours:
        area = float(cv2.contourArea(contour))
        if area < min_candidate_area:
            rejection_counts["area_ratio"] += 1
            continue
        hull = cv2.convexHull(contour)
        hull_area = float(cv2.contourArea(hull))
        if hull_area <= 0:
            rejection_counts["solidity"] += 1
            continue
        solidity = area / hull_area
        if solidity < float(cfg["min_solidity"]):
            rejection_counts["solidity"] += 1
            continue

        perimeter = float(cv2.arcLength(contour, True))
        approximation = cv2.approxPolyDP(
            contour,
            float(cfg["approx_epsilon_fraction"]) * perimeter,
            True,
        )
        if len(approximation) != 4 or not cv2.isContourConvex(approximation):
            rejection_counts["aspect_ratio"] += 1
            continue

        quad = _order_quad(approximation.reshape(4, 2))
        if len(np.unique(quad, axis=0)) < 4:
            rejection_counts["area_ratio"] += 1
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
        rejection_counts=rejection_counts,
    )
    if bright_candidate is not None and (
        best is None or float(bright_candidate["area_ratio"]) > float(best["area_ratio"]) * 1.2
    ):
        best = bright_candidate

    if best is None:
        hough_candidate = _hough_rectangle_candidate(
            blurred,
            width=width,
            height=height,
            cfg=cfg,
            tolerance=tolerance,
            edge_density=edge_density,
            rejection_counts=rejection_counts,
        )
        if hough_candidate is not None:
            best = hough_candidate
            candidate_count = max(
                candidate_count,
                int(hough_candidate.get("candidate_count", 0)),
            )

    border_cut_edge_density = cfg.get("border_cut_full_frame_edge_density")
    content_border_sides = 0
    if best is None:
        outside_edge_density = _outside_edge_density(
            gray,
            central_fraction=central_fraction,
            canny_low=int(cfg["canny_low"]),
            canny_high=int(cfg["canny_high"]),
        )
        minimum_edge_density = float(cfg["no_document_min_edge_density"])
        if (
            edge_density >= minimum_edge_density
            and outside_edge_density < minimum_edge_density
        ):
            best = _center_crop_candidate(
                width=width,
                height=height,
                edge_density=edge_density,
                cfg=cfg,
            )
        if border_cut_edge_density is not None:
            content_border_sides = _border_content_sides(
                gray,
                canny_low=int(cfg["canny_low"]),
                canny_high=int(cfg["canny_high"]),
                strip_fraction=float(cfg["border_tolerance_fraction"]),
                minimum_density=minimum_edge_density,
            )

    # Driving-license cards are often held close to the camera and cut off by
    # the frame.  When there are no closed-contour candidates but high-contrast
    # content reaches at least two frame edges, treat the full frame as the
    # document and record why cropping was not enforced.
    effective_border_sides = (
        int(best["border_sides"]) if best is not None else content_border_sides
    )
    if (
        border_cut_edge_density is not None
        and candidate_count == 0
        and effective_border_sides >= 2
        and edge_density > float(border_cut_edge_density)
    ):
        source_best = best if best is not None else {
            "confidence": 0.60,
            "candidate_count": candidate_count,
        }
        best = _full_frame_candidate(
            width=width,
            height=height,
            edge_density=edge_density,
            best=source_best,
            warning="border_cut_but_high_contrast",
        )

    if best is None and candidate_count == 0:
        adaptive_candidate = _adaptive_threshold_rectangle_candidate(
            gray,
            width=width,
            height=height,
            edge_density=edge_density,
            cfg=cfg,
            tolerance=tolerance,
            rejection_counts=rejection_counts,
        )
        if adaptive_candidate is not None:
            best = adaptive_candidate
            candidate_count = int(adaptive_candidate.get("candidate_count", 1))

    if best is None:
        clear_rectangle, rectangle_edges = _strong_rectangle_edges(gray, cfg)
        rejection_counts["none_found"] += 1
        return _empty_boundary(
            edge_density,
            candidate_count,
            cfg,
            border_sides_touching=content_border_sides,
            rejection_counts=rejection_counts,
            clear_rectangle_heuristic=clear_rectangle,
            rectangle_edge_counts=rectangle_edges,
        )

    # If the detected card fills more than 80% of the frame, use the whole
    # frame as the document region.  Frame borders are not cropping evidence in
    # this fallback, so the cropping flags are deliberately cleared.
    if (
        best.get("method") != "full_frame"
        and float(best["area_ratio"]) > 0.8
        and edge_density >= float(cfg["no_document_min_edge_density"])
    ):
        best = _full_frame_candidate(
            width=width,
            height=height,
            edge_density=edge_density,
            best=best,
        )

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
        clear_rectangle, rectangle_edges = _strong_rectangle_edges(gray, cfg)
        rejection_counts["border_contact"] += 1
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
            rejection_counts=_rejection_payload(rejection_counts),
            clear_rectangle_heuristic=clear_rectangle,
            rectangle_edge_counts=rectangle_edges,
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

    warning_value = best.get("warning")
    warning = str(warning_value) if warning_value else None
    cropping_suspected = border_sides >= int(cfg["cropping_min_sides"])
    cropping_confirmed = (
        cropping_suspected
        and valid
        and confidence >= float(cfg["cropping_min_confidence"])
    )
    if not valid:
        rejection_counts["confidence"] += 1
    if aspect_in_range is False:
        rejection_counts["aspect_ratio"] += 1
    if cropping_suspected:
        rejection_counts["border_contact"] += 1

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
        warning=warning,
        rejection_counts=_rejection_payload(rejection_counts),
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
