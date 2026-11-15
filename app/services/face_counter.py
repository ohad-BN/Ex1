from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
import shutil
import tempfile
from typing import Any, Optional

import cv2
import numpy as np


@dataclass
class BoundingBox:
    """Detected or lecturer-verified student face bounding box."""

    label_id: int
    x1: float
    y1: float
    x2: float
    y2: float
    confidence: float = 1.0
    source: str = "auto"

    def iou(self, other: "BoundingBox") -> float:
        """Compute Intersection-over-Union with another BoundingBox."""
        ix1 = max(self.x1, other.x1)
        iy1 = max(self.y1, other.y1)
        ix2 = min(self.x2, other.x2)
        iy2 = min(self.y2, other.y2)
        inter = max(0.0, ix2 - ix1) * max(0.0, iy2 - iy1)
        area_self = max(0.0, self.x2 - self.x1) * max(0.0, self.y2 - self.y1)
        area_other = max(0.0, other.x2 - other.x1) * max(0.0, other.y2 - other.y1)
        union = area_self + area_other - inter
        return inter / union if union > 0.0 else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "label_id": int(self.label_id),
            "x1": round(float(self.x1), 1),
            "y1": round(float(self.y1), 1),
            "x2": round(float(self.x2), 1),
            "y2": round(float(self.y2), 1),
            "confidence": round(float(self.confidence), 3),
            "source": str(self.source),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any], default_id: int = 1) -> "BoundingBox":
        return cls(
            label_id=int(data.get("label_id", default_id)),
            x1=float(data["x1"]),
            y1=float(data["y1"]),
            x2=float(data["x2"]),
            y2=float(data["y2"]),
            confidence=float(data.get("confidence", 1.0)),
            source=str(data.get("source", "auto")),
        )


def read_image_unicode(path: str | Path) -> np.ndarray:
    """Read an image from disk supporting Windows paths with Hebrew/Unicode characters."""
    file_path = Path(path)
    if not file_path.exists():
        raise FileNotFoundError(f"Image not found: {file_path}")
    raw_bytes = np.fromfile(str(file_path), dtype=np.uint8)
    img = cv2.imdecode(raw_bytes, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError(f"Failed to decode image file: {file_path}")
    return img


def write_image_unicode(path: str | Path, image: np.ndarray) -> str:
    """Write an image to disk supporting Windows paths with Hebrew/Unicode characters."""
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    ext = file_path.suffix.lower() or ".jpg"
    ok, buf = cv2.imencode(ext, image)
    if not ok:
        raise RuntimeError(f"Failed to encode image for {file_path}")
    buf.tofile(str(file_path))
    return str(file_path)


class FaceDetector(ABC):
    """Abstract interface for classroom head/face counting engines."""

    @abstractmethod
    def detect(self, image: np.ndarray) -> list[BoundingBox]:
        """Return deduplicated list of face bounding boxes in image coordinates."""


class TiledSCRFDDetector(FaceDetector):
    """InsightFace SCRFD (det_10g ONNX) detector with overlapping 3x2 tiling and IoU NMS.

    Specifically engineered to detect both large front-row faces and tiny 15px back-row
    faces in wide-angle university lecture halls on CPU.
    """

    def __init__(
        self,
        model_path: Optional[str | Path] = None,
        conf_threshold: float = 0.35,
        nms_iou_threshold: float = 0.40,
        grid_shape: tuple[int, int] = (3, 2),
        tile_overlap: float = 0.20,
    ) -> None:
        self.model_path = Path(model_path) if model_path else None
        self.conf_threshold = conf_threshold
        self.nms_iou_threshold = nms_iou_threshold
        self.grid_shape = grid_shape
        self.tile_overlap = tile_overlap
        self._detector: Any = None

    def _ensure_detector(self) -> Any:
        if self._detector is not None:
            return self._detector

        from insightface.model_zoo import get_model

        candidates = []
        if self.model_path:
            candidates.append(self.model_path)
        root_dir = Path(__file__).resolve().parent.parent.parent
        candidates.extend(
            [
                root_dir / "models_cache" / "det_10g.onnx",
                Path.home() / ".insightface" / "models" / "buffalo_l" / "det_10g.onnx",
            ]
        )

        onnx_file: Optional[Path] = next((p for p in candidates if p.exists()), None)
        if onnx_file is not None:
            # Copy to an ASCII-safe temp path on Windows in case workspace contains Hebrew chars
            ascii_model_path = Path(tempfile.gettempdir()) / "classpulse_det_10g.onnx"
            if not ascii_model_path.exists() or ascii_model_path.stat().st_size != onnx_file.stat().st_size:
                shutil.copy2(onnx_file, ascii_model_path)
            det = get_model(str(ascii_model_path), providers=["CPUExecutionProvider"])
            det.prepare(ctx_id=-1, input_size=(640, 640), det_thresh=self.conf_threshold)
            self._detector = ("raw_model", det)
            return self._detector

        from insightface.app import FaceAnalysis

        fa = FaceAnalysis(
            name="buffalo_l",
            root=str(Path.home() / ".insightface"),
            allowed_modules=["detection"],
            providers=["CPUExecutionProvider"],
        )
        fa.prepare(ctx_id=-1, det_size=(640, 640))
        self._detector = ("face_analysis", fa)
        return self._detector

    def _run_scrfd_crop(self, crop: np.ndarray) -> list[tuple[float, float, float, float, float]]:
        mode, det = self._ensure_detector()
        results: list[tuple[float, float, float, float, float]] = []
        if mode == "raw_model":
            bboxes, _kpss = det.detect(crop, input_size=(640, 640), max_num=0, metric="default")
            if bboxes is not None:
                for row in bboxes:
                    score = float(row[4])
                    if score >= self.conf_threshold:
                        results.append(
                            (float(row[0]), float(row[1]), float(row[2]), float(row[3]), score)
                        )
        else:
            for face in det.get(crop):
                score = float(face.det_score)
                if score >= self.conf_threshold:
                    b = face.bbox
                    results.append((float(b[0]), float(b[1]), float(b[2]), float(b[3]), score))
        return results

    def _generate_tiles(self, width: int, height: int) -> list[tuple[int, int, int, int]]:
        """Generate overlapping spatial tile coordinates (x1, y1, x2, y2)."""
        if width < 800 or height < 600:
            return []
        cols, rows = self.grid_shape
        step_x = int(width / cols)
        step_y = int(height / rows)
        pad_x = int(step_x * self.tile_overlap)
        pad_y = int(step_y * self.tile_overlap)

        tiles: list[tuple[int, int, int, int]] = []
        for r in range(rows):
            for c in range(cols):
                x1 = max(0, c * step_x - pad_x)
                y1 = max(0, r * step_y - pad_y)
                x2 = min(width, (c + 1) * step_x + pad_x)
                y2 = min(height, (r + 1) * step_y + pad_y)
                tiles.append((x1, y1, x2, y2))
        return tiles

    def _apply_nms(self, boxes: list[BoundingBox]) -> list[BoundingBox]:
        """Deduplicate overlapping detections across tile borders using IoU NMS."""
        ordered = sorted(boxes, key=lambda b: b.confidence, reverse=True)
        kept: list[BoundingBox] = []
        for candidate in ordered:
            if all(candidate.iou(existing) < self.nms_iou_threshold for existing in kept):
                kept.append(candidate)
        for idx, box in enumerate(kept, start=1):
            box.label_id = idx
        return kept

    def detect(self, image: np.ndarray) -> list[BoundingBox]:
        """Run full-frame + overlapping tiled SCRFD detection and return NMS-merged boxes."""
        if image is None or image.size == 0:
            return []
        height, width = image.shape[:2]
        candidates: list[BoundingBox] = []

        # 1. Full-frame detection pass
        for x1, y1, x2, y2, score in self._run_scrfd_crop(image):
            candidates.append(
                BoundingBox(
                    label_id=0,
                    x1=max(0.0, x1),
                    y1=max(0.0, y1),
                    x2=min(float(width), x2),
                    y2=min(float(height), y2),
                    confidence=score,
                    source="auto",
                )
            )

        # 2. Overlapping tile detection passes for small back-row faces
        for tx1, ty1, tx2, ty2 in self._generate_tiles(width, height):
            crop = image[ty1:ty2, tx1:tx2]
            for bx1, by1, bx2, by2, score in self._run_scrfd_crop(crop):
                candidates.append(
                    BoundingBox(
                        label_id=0,
                        x1=max(0.0, bx1 + tx1),
                        y1=max(0.0, by1 + ty1),
                        x2=min(float(width), bx2 + tx1),
                        y2=min(float(height), by2 + ty1),
                        confidence=score,
                        source="auto",
                    )
                )

        return self._apply_nms(candidates)

    @staticmethod
    def render_annotated_image(
        image_path: str | Path,
        boxes: list[BoundingBox],
        out_path: str | Path,
    ) -> str:
        """Render numbered bounding boxes onto the classroom image and save to out_path."""
        img = read_image_unicode(image_path)
        canvas = img.copy()
        for idx, box in enumerate(boxes, start=1):
            box.label_id = idx
            x1, y1, x2, y2 = (
                int(round(box.x1)),
                int(round(box.y1)),
                int(round(box.x2)),
                int(round(box.y2)),
            )
            color = (0, 230, 118) if box.source == "auto" else (255, 193, 7)
            cv2.rectangle(canvas, (x1, y1), (x2, y2), color, 2)
            cv2.putText(
                canvas,
                str(idx),
                (x1, max(16, y1 - 5)),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.55,
                color,
                2,
            )
        return write_image_unicode(out_path, canvas)
