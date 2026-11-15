# Real-Life Tool Verification Results (Phase 0 — Ex2 To-Do #3)

**Executed On:** 2026-11-15 13:19:06  
**Host Environment:** Python 3.12.10 (Windows CPU ExecutionProvider)

---

## Summary Table

| Test ID | Tool / Subsystem | Measured Result | Verdict |
| :--- | :--- | :--- | :--- |
| **TV-01** | **InsightFace SCRFD (`buffalo_l` / `det_10g` ONNX) on Ariel Lecture Hall Photo (`2000x1500`)** | Single-pass: **21 faces** (0.161s) vs Tiled 3x2 + NMS: **34 faces** (0.466s). Annotated output saved to `data\samples\classroom_ariel_annotated.jpg`. | **PASS** |
| **TV-02** | **Flask + `ProxyFix` + `Flask-SocketIO` (`threading`)** | HTTP `200`, forwarded scheme=`https`, host=`demo-class.trycloudflare.com`, Socket.IO connected=`True`. | **PASS** |
| **TV-03** | **5-Second Rolling HMAC-SHA256 Grace Window (`t`, `t-1`)** | Cross-boundary scan (`4.8s -> 6.2s`) accepted=`True`; expired scan (`>10s`) rejected=`True`. | **PASS** |
