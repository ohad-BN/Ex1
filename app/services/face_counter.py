from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import tempfile
import threading
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
    """Read an image from disk supporting Unicode paths, EXIF orientation, and WebP/PNG modes."""
    file_path = Path(path)
    if not file_path.exists():
        raise FileNotFoundError(f"Image not found: {file_path}")
    raw_bytes = np.fromfile(str(file_path), dtype=np.uint8)
    try:
        import io
        from PIL import Image, ImageOps

        with Image.open(io.BytesIO(raw_bytes)) as pil_img:
            pil_img = ImageOps.exif_transpose(pil_img)
            if pil_img.mode != "RGB":
                pil_img = pil_img.convert("RGB")
            rgb = np.asarray(pil_img)
            if rgb.size > 0:
                return cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
    except Exception:
        pass
    img = cv2.imdecode(raw_bytes, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError(f"Failed to decode image file: {file_path}")
    return img


def write_image_unicode(path: str | Path, image: np.ndarray) -> str:
    """Write an image to disk supporting Windows paths with Hebrew/Unicode characters."""
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    ext = file_path.suffix.lower() or ".jpg"
    encode_params = [int(cv2.IMWRITE_JPEG_QUALITY), 85] if ext in (".jpg", ".jpeg") else []
    ok, buf = cv2.imencode(ext, image, encode_params)
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
        conf_threshold: float = 0.30,
        nms_iou_threshold: float = 0.40,
        grid_shape: tuple[int, int] = (3, 2),
        tile_overlap: float = 0.20,
        fallback_conf_threshold: float = 0.20,
    ) -> None:
        self.model_path = Path(model_path) if model_path else None
        self.conf_threshold = conf_threshold
        self.fallback_conf_threshold = min(conf_threshold, fallback_conf_threshold)
        self.nms_iou_threshold = nms_iou_threshold
        self.grid_shape = grid_shape
        self.tile_overlap = tile_overlap
        self._detector: Any = None
        self._lock = threading.Lock()

    def _ensure_detector_locked(self) -> Any:
        if self._detector is not None:
            return self._detector

        import onnxruntime as ort
        from insightface.model_zoo.scrfd import SCRFD

        candidates: list[Path] = []
        if self.model_path:
            candidates.append(self.model_path)
        env_home = os.getenv("INSIGHTFACE_HOME", "").strip()
        if env_home:
            eh = Path(env_home)
            candidates.extend(
                [
                    eh / "det_10g.onnx",
                    eh / "models" / "buffalo_l" / "det_10g.onnx",
                ]
            )
        root_dir = Path(__file__).resolve().parent.parent.parent
        candidates.extend(
            [
                root_dir / "models_cache" / "det_10g.onnx",
                root_dir / "models_cache" / "models" / "buffalo_l" / "det_10g.onnx",
                Path.home() / ".insightface" / "models" / "buffalo_l" / "det_10g.onnx",
            ]
        )

        onnx_file: Optional[Path] = next((p for p in candidates if p.exists()), None)
        if onnx_file is None:
            raise FileNotFoundError(
                "SCRFD face detection model det_10g.onnx not found in models_cache."
            )

        # Copy atomically to an ASCII-safe temp path on Windows in case workspace contains Hebrew chars
        ascii_model_path = Path(tempfile.gettempdir()) / "classpulse_det_10g.onnx"
        if not ascii_model_path.exists() or ascii_model_path.stat().st_size != onnx_file.stat().st_size:
            tmp_onnx = ascii_model_path.with_suffix(".onnx.tmp")
            shutil.copy2(onnx_file, tmp_onnx)
            os.replace(tmp_onnx, ascii_model_path)

        sess_opts = ort.SessionOptions()
        sess_opts.intra_op_num_threads = min(4, max(1, os.cpu_count() or 2))
        sess_opts.inter_op_num_threads = 1
        sess_opts.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL

        ort_session = ort.InferenceSession(
            str(ascii_model_path),
            sess_options=sess_opts,
            providers=["CPUExecutionProvider"],
        )
        det = SCRFD(model_file=str(ascii_model_path), session=ort_session)
        det.prepare(ctx_id=-1, input_size=(640, 640), det_thresh=self.conf_threshold)
        self._detector = ("raw_model", det)
        return self._detector

    def _ensure_detector(self) -> Any:
        with self._lock:
            return self._ensure_detector_locked()

    def warmup(self) -> bool:
        """Preload the ONNX SCRFD detector so the first mobile upload has no cold-start lag."""
        try:
            with self._lock:
                self._ensure_detector_locked()
            return True
        except Exception:  # noqa: BLE001
            return False

    def _run_scrfd_crop_locked(
        self, crop: np.ndarray, threshold: Optional[float] = None
    ) -> list[tuple[float, float, float, float, float]]:
        mode, det = self._ensure_detector_locked()
        eff_thresh = self.conf_threshold if threshold is None else float(threshold)
        results: list[tuple[float, float, float, float, float]] = []
        if mode == "raw_model":
            bboxes, _kpss = det.detect(
                crop,
                input_size=(640, 640),
                max_num=0,
                metric="default",
                det_thresh=eff_thresh,
            )
            if bboxes is not None:
                for row in bboxes:
                    score = float(row[4])
                    if score >= eff_thresh:
                        results.append(
                            (float(row[0]), float(row[1]), float(row[2]), float(row[3]), score)
                        )
        else:
            for face in det.get(crop):
                score = float(face.det_score)
                if score >= eff_thresh:
                    b = face.bbox
                    results.append((float(b[0]), float(b[1]), float(b[2]), float(b[3]), score))
        return results

    def _run_scrfd_crop(
        self, crop: np.ndarray, threshold: Optional[float] = None
    ) -> list[tuple[float, float, float, float, float]]:
        with self._lock:
            return self._run_scrfd_crop_locked(crop, threshold=threshold)

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

    def _detect_at_threshold_locked(
        self, image: np.ndarray, threshold: float, *, include_tiles: bool = True
    ) -> list[BoundingBox]:
        """Run full-frame + overlapping tiled SCRFD detection on a fast 1280px working scale."""
        orig_h, orig_w = image.shape[:2]
        longest = max(orig_h, orig_w)
        max_work_edge = 1280.0
        if longest > max_work_edge:
            scale = max_work_edge / float(longest)
            work_w = max(1, int(round(orig_w * scale)))
            work_h = max(1, int(round(orig_h * scale)))
            work_img = cv2.resize(image, (work_w, work_h), interpolation=cv2.INTER_AREA)
            inv_scale = 1.0 / scale
        else:
            work_img = image
            work_w, work_h = orig_w, orig_h
            inv_scale = 1.0

        candidates: list[BoundingBox] = []

        # 1. Full-frame detection pass
        for x1, y1, x2, y2, score in self._run_scrfd_crop_locked(work_img, threshold=threshold):
            candidates.append(
                BoundingBox(
                    label_id=0,
                    x1=max(0.0, x1 * inv_scale),
                    y1=max(0.0, y1 * inv_scale),
                    x2=min(float(orig_w), x2 * inv_scale),
                    y2=min(float(orig_h), y2 * inv_scale),
                    confidence=score,
                    source="auto",
                )
            )

        # 2. Overlapping tile detection passes for small back-row faces in wide shots
        if include_tiles:
            for tx1, ty1, tx2, ty2 in self._generate_tiles(work_w, work_h):
                crop = work_img[ty1:ty2, tx1:tx2]
                for bx1, by1, bx2, by2, score in self._run_scrfd_crop_locked(crop, threshold=threshold):
                    candidates.append(
                        BoundingBox(
                            label_id=0,
                            x1=max(0.0, (bx1 + tx1) * inv_scale),
                            y1=max(0.0, (by1 + ty1) * inv_scale),
                            x2=min(float(orig_w), (bx2 + tx1) * inv_scale),
                            y2=min(float(orig_h), (by2 + ty1) * inv_scale),
                            confidence=score,
                            source="auto",
                        )
                    )

        return self._apply_nms(candidates)

    def _detect_padded_closeup_locked(
        self,
        image: np.ndarray,
        threshold: float = 0.20,
        pad_ratio: float = 0.35,
    ) -> list[BoundingBox]:
        """Fast border-padded pass catching close-up portraits/selfies where face fills frame."""
        orig_h, orig_w = image.shape[:2]
        longest = max(orig_h, orig_w, 1)
        scale = min(1.0, 720.0 / float(longest))
        work = (
            cv2.resize(
                image,
                (max(1, int(round(orig_w * scale))), max(1, int(round(orig_h * scale)))),
                interpolation=cv2.INTER_AREA,
            )
            if scale < 1.0
            else image
        )
        wh, ww = work.shape[:2]
        pad_y = max(48, int(round(wh * pad_ratio)))
        pad_x = max(48, int(round(ww * pad_ratio)))
        padded = cv2.copyMakeBorder(
            work, pad_y, pad_y, pad_x, pad_x, cv2.BORDER_CONSTANT, value=(128, 128, 128)
        )
        inv_scale = 1.0 / scale
        candidates: list[BoundingBox] = []
        for x1, y1, x2, y2, score in self._run_scrfd_crop_locked(padded, threshold=threshold):
            ox1 = max(0.0, (x1 - pad_x) * inv_scale)
            oy1 = max(0.0, (y1 - pad_y) * inv_scale)
            ox2 = min(float(orig_w), (x2 - pad_x) * inv_scale)
            oy2 = min(float(orig_h), (y2 - pad_y) * inv_scale)
            if ox2 > ox1 + 4.0 and oy2 > oy1 + 4.0:
                candidates.append(
                    BoundingBox(
                        label_id=0,
                        x1=ox1,
                        y1=oy1,
                        x2=ox2,
                        y2=oy2,
                        confidence=score,
                        source="auto",
                    )
                )
        return self._apply_nms(candidates)

    def _detect_upscaled_locked(
        self, image: np.ndarray, threshold: float = 0.18, factor: float = 1.6
    ) -> list[BoundingBox]:
        """Upscale small/webcam photos so SCRFD catches smaller or slightly blurred faces."""
        orig_h, orig_w = image.shape[:2]
        if max(orig_h, orig_w) >= 850:
            return []
        up_w = max(1, int(round(orig_w * factor)))
        up_h = max(1, int(round(orig_h * factor)))
        up_img = cv2.resize(image, (up_w, up_h), interpolation=cv2.INTER_CUBIC)
        inv = 1.0 / factor
        candidates: list[BoundingBox] = []
        for x1, y1, x2, y2, score in self._run_scrfd_crop_locked(up_img, threshold=threshold):
            candidates.append(
                BoundingBox(
                    label_id=0,
                    x1=max(0.0, x1 * inv),
                    y1=max(0.0, y1 * inv),
                    x2=min(float(orig_w), x2 * inv),
                    y2=min(float(orig_h), y2 * inv),
                    confidence=score,
                    source="auto",
                )
            )
        return self._apply_nms(candidates)

    def detect(
        self, image: np.ndarray, *, conf_threshold: Optional[float] = None
    ) -> list[BoundingBox]:
        """Run full-frame + overlapping tiled SCRFD detection plus close-up, upscale & lighting recovery."""
        if image is None or image.size == 0:
            return []
        eff_thresh = self.conf_threshold if conf_threshold is None else float(conf_threshold)
        with self._lock:
            boxes = self._detect_at_threshold_locked(image, eff_thresh, include_tiles=True)
            if not boxes and conf_threshold is None:
                boxes = self._detect_at_threshold_locked(
                    image, self.fallback_conf_threshold, include_tiles=True
                )
            if not boxes and conf_threshold is None:
                boxes = self._detect_padded_closeup_locked(image, threshold=0.16, pad_ratio=0.35)
            if not boxes and conf_threshold is None:
                boxes = self._detect_padded_closeup_locked(image, threshold=0.15, pad_ratio=0.60)
            if not boxes and conf_threshold is None:
                boxes = self._detect_upscaled_locked(image, threshold=0.16, factor=1.6)
            if not boxes and conf_threshold is None:
                try:
                    lab = cv2.cvtColor(image, cv2.COLOR_BGR2LAB)
                    l_ch, a_ch, b_ch = cv2.split(lab)
                    clahe = cv2.createCLAHE(clipLimit=2.4, tileGridSize=(8, 8))
                    enhanced = cv2.cvtColor(
                        cv2.merge((clahe.apply(l_ch), a_ch, b_ch)), cv2.COLOR_LAB2BGR
                    )
                    boxes = self._detect_at_threshold_locked(enhanced, 0.16, include_tiles=True)
                    if not boxes:
                        boxes = self._detect_padded_closeup_locked(enhanced, threshold=0.14, pad_ratio=0.45)
                except Exception:
                    pass
            if not boxes and conf_threshold is None:
                try:
                    table = np.array(
                        [min(255, int(((i / 255.0) ** (1.0 / 1.5)) * 255.0)) for i in range(256)],
                        dtype=np.uint8,
                    )
                    bright = cv2.LUT(image, table)
                    boxes = self._detect_padded_closeup_locked(bright, threshold=0.15, pad_ratio=0.40)
                except Exception:
                    pass
            return boxes

    def detect_with_orientation(
        self, image: np.ndarray
    ) -> tuple[list[BoundingBox], np.ndarray, bool]:
        """Detect upright high-confidence faces first; probe 90/270/180 before extreme low-threshold fallback."""
        if image is None or image.size == 0:
            return [], image, False

        with self._lock:
            upright_full = self._detect_at_threshold_locked(
                image, self.conf_threshold, include_tiles=False
            )
            if len(upright_full) >= 3:
                upright_boxes = self._detect_at_threshold_locked(
                    image, self.conf_threshold, include_tiles=True
                )
                return upright_boxes, image, False
            upright_boxes = list(upright_full)
            if not upright_boxes:
                upright_boxes = self._detect_padded_closeup_locked(
                    image, threshold=self.conf_threshold, pad_ratio=0.35
                )

        best_boxes: list[BoundingBox] = list(upright_boxes)
        best_img: np.ndarray = image
        best_rot: bool = False
        best_score: tuple[int, float] = (
            len(upright_boxes),
            sum(b.confidence for b in upright_boxes),
        )

        for rot_code in (
            cv2.ROTATE_90_CLOCKWISE,
            cv2.ROTATE_90_COUNTERCLOCKWISE,
            cv2.ROTATE_180,
        ):
            rot_img = cv2.rotate(image, rot_code)
            with self._lock:
                probe_boxes = self._detect_at_threshold_locked(
                    rot_img, min(0.28, self.conf_threshold), include_tiles=False
                )
                if not probe_boxes:
                    probe_boxes = self._detect_padded_closeup_locked(
                        rot_img, threshold=min(0.25, self.conf_threshold), pad_ratio=0.35
                    )
            if not probe_boxes:
                continue

            rot_boxes = self.detect(rot_img)
            score_key = (len(rot_boxes), sum(b.confidence for b in rot_boxes))
            if score_key > best_score:
                best_score = score_key
                best_boxes = rot_boxes
                best_img = rot_img
                best_rot = True

        if best_boxes:
            return best_boxes, best_img, best_rot

        # Only after normal orientation & rotations fail, run deep close-up / contrast / gamma recovery upright
        fallback_boxes = self.detect(image)
        return fallback_boxes, image, False

    @staticmethod
    def render_annotated_image(
        image_path: str | Path,
        boxes: list[BoundingBox],
        out_path: str | Path,
        image: Optional[np.ndarray] = None,
    ) -> str:
        """Render numbered bounding boxes onto the classroom image and save to out_path."""
        img = image if image is not None else read_image_unicode(image_path)
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
