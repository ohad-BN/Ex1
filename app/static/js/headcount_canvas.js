class HeadcountCanvasEditor {
  constructor(canvasId, sessionId, onUpdate) {
    this.canvas = document.getElementById(canvasId);
    this.sessionId = sessionId;
    this.onUpdate = onUpdate || (() => {});
    this.currentBoxes = [];
    this.loadedImg = null;
    this.autoCount = 0;

    if (this.canvas) {
      this.ctx = this.canvas.getContext("2d");
      this.canvas.addEventListener("click", (evt) => this.handleClick(evt));
    }
  }

  drawCanvas() {
    if (!this.canvas || !this.ctx || !this.loadedImg) return;
    this.canvas.width = this.loadedImg.naturalWidth;
    this.canvas.height = this.loadedImg.naturalHeight;
    this.ctx.clearRect(0, 0, this.canvas.width, this.canvas.height);
    this.ctx.drawImage(this.loadedImg, 0, 0);

    this.currentBoxes.forEach((box, i) => {
      box.label_id = i + 1;
      const isAuto = box.source !== "manual_added";
      this.ctx.strokeStyle = isAuto ? "#00e676" : "#fbbf24";
      this.ctx.lineWidth = 3;
      const w = box.x2 - box.x1;
      const h = box.y2 - box.y1;
      this.ctx.strokeRect(box.x1, box.y1, w, h);

      this.ctx.fillStyle = isAuto ? "#00e676" : "#fbbf24";
      this.ctx.font = "bold 20px sans-serif";
      this.ctx.fillText(String(box.label_id), box.x1, Math.max(22, box.y1 - 6));
    });

    const verifiedEl = document.getElementById("kpi-visual-count");
    if (verifiedEl) verifiedEl.textContent = String(this.currentBoxes.length);
  }

  loadSnapshot(snapshotOrUrl, boxesArg) {
    if (!this.canvas) return;
    let imageUrl = "";
    let boxes = [];

    if (typeof snapshotOrUrl === "string") {
      imageUrl = snapshotOrUrl;
      boxes = Array.isArray(boxesArg) ? boxesArg : [];
    } else if (snapshotOrUrl && typeof snapshotOrUrl === "object") {
      imageUrl = snapshotOrUrl.image_url || "";
      boxes = Array.isArray(snapshotOrUrl.boxes) ? snapshotOrUrl.boxes : [];
      this.autoCount = snapshotOrUrl.auto_count || boxes.length;
    }

    if (!imageUrl) return;
    this.currentBoxes = boxes.map((b) => ({ ...b }));
    const img = new Image();
    img.onload = () => {
      this.loadedImg = img;
      this.canvas.style.display = "block";
      const emptyHint = document.getElementById("canvas-empty-hint");
      if (emptyHint) emptyHint.style.display = "none";
      this.drawCanvas();
    };
    img.src = imageUrl + (imageUrl.includes("?") ? "&" : "?") + "v=" + Date.now();
  }

  async syncBoxesToServer() {
    if (!this.sessionId) return;
    const res = await fetch(`/api/sessions/${this.sessionId}/headcount`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ boxes: this.currentBoxes }),
    });
    if (!res.ok) return;
    const data = await res.json();
    if (this.onUpdate) {
      this.onUpdate({
        auto_count: data.auto_count ?? this.autoCount,
        verified_count: data.verified_count ?? this.currentBoxes.length,
        verdict: data.verdict,
      });
    }
    if (window.updateAuditVerdictUI && data.verdict) {
      window.updateAuditVerdictUI(data.verdict);
    }
  }

  handleClick(evt) {
    if (!this.loadedImg || !this.canvas) return;
    const rect = this.canvas.getBoundingClientRect();
    const scaleX = this.canvas.width / rect.width;
    const scaleY = this.canvas.height / rect.height;
    const clickX = (evt.clientX - rect.left) * scaleX;
    const clickY = (evt.clientY - rect.top) * scaleY;

    const hitIdx = this.currentBoxes.findIndex(
      (b) => clickX >= b.x1 && clickX <= b.x2 && clickY >= b.y1 && clickY <= b.y2
    );

    if (hitIdx >= 0) {
      this.currentBoxes.splice(hitIdx, 1);
    } else {
      const half = 22;
      this.currentBoxes.push({
        label_id: this.currentBoxes.length + 1,
        x1: Math.max(0, clickX - half),
        y1: Math.max(0, clickY - half),
        x2: Math.min(this.canvas.width, clickX + half),
        y2: Math.min(this.canvas.height, clickY + half),
        confidence: 1.0,
        source: "manual_added",
      });
    }
    this.drawCanvas();
    this.syncBoxesToServer();
  }
}

window.HeadcountCanvasEditor = HeadcountCanvasEditor;

(() => {
  const canvas = document.getElementById("headcount-canvas");
  if (!canvas) return;
  const sessionId = canvas.dataset.sessionId;
  if (sessionId) {
    const defaultEditor = new HeadcountCanvasEditor("headcount-canvas", sessionId);
    window.initHeadcountCanvas = (url, boxes) => defaultEditor.loadSnapshot(url, boxes);
  }
})();
