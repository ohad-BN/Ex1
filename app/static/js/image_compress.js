(function () {
  function isHeicLike(file) {
    const type = String((file && file.type) || "").toLowerCase();
    const name = String((file && file.name) || "").toLowerCase();
    return (
      type.includes("heic") ||
      type.includes("heif") ||
      name.endsWith(".heic") ||
      name.endsWith(".heif")
    );
  }

  function decodeViaHtmlImage(file, timeoutMs) {
    return new Promise((resolve, reject) => {
      const objUrl = URL.createObjectURL(file);
      let settled = false;
      const timer = setTimeout(() => {
        if (settled) return;
        settled = true;
        URL.revokeObjectURL(objUrl);
        reject(new Error("Timed out decoding image."));
      }, timeoutMs);

      const img = new Image();
      img.onload = () => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        const w = img.naturalWidth || img.width;
        const h = img.naturalHeight || img.height;
        resolve({
          source: img,
          width: w,
          height: h,
          cleanup: () => URL.revokeObjectURL(objUrl),
        });
      };
      img.onerror = () => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        URL.revokeObjectURL(objUrl);
        reject(new Error("Could not decode image via Image element."));
      };
      img.src = objUrl;
    });
  }

  async function decodeViaBitmap(file, timeoutMs) {
    if (typeof createImageBitmap !== "function") {
      throw new Error("createImageBitmap unavailable");
    }
    let timer = null;
    try {
      const bitmap = await Promise.race([
        createImageBitmap(file, { imageOrientation: "from-image" }),
        new Promise((_, reject) => {
          timer = setTimeout(() => reject(new Error("Timed out decoding bitmap.")), timeoutMs);
        }),
      ]);
      return {
        source: bitmap,
        width: bitmap.width,
        height: bitmap.height,
        cleanup: () => {
          if (bitmap && typeof bitmap.close === "function") bitmap.close();
        },
      };
    } finally {
      if (timer) clearTimeout(timer);
    }
  }

  async function compressToJpeg(file, opts) {
    const cfg = opts || {};
    const maxEdge = Number(cfg.maxEdge || 1280);
    const quality = Number(cfg.quality || 0.80);
    const decodeTimeoutMs = Number(cfg.decodeTimeoutMs || 6000);

    if (!file) {
      throw new Error("No image file selected.");
    }

    const lowerName = String(file.name || "").toLowerCase();
    const isStandardDirectImage =
      !isHeicLike(file) &&
      (String(file.type || "").startsWith("image/") ||
        /\.(jpe?g|png|webp|gif)$/i.test(lowerName));

    let decoded = null;
    try {
      decoded = await decodeViaBitmap(file, decodeTimeoutMs);
    } catch (_bmpErr) {
      try {
        decoded = await decodeViaHtmlImage(file, decodeTimeoutMs);
      } catch (_htmlErr) {
        if (isStandardDirectImage) {
          return file;
        }
        if (isHeicLike(file)) {
          throw new Error(
            "This HEIC/HEIF photo could not be decoded in the browser. Please take a photo directly with the camera or choose a JPEG/PNG image."
          );
        }
        throw new Error(
          "Could not process this image file. Please try retaking the photo or selecting a JPEG/PNG image."
        );
      }
    }

    try {
      const width = Number(decoded.width || 0);
      const height = Number(decoded.height || 0);
      if (width <= 0 || height <= 0) {
        if (isStandardDirectImage) return file;
        throw new Error("Image has zero dimensions.");
      }

      const longest = Math.max(width, height, 1);
      const scale = longest > maxEdge ? maxEdge / longest : 1;
      const targetW = Math.max(1, Math.round(width * scale));
      const targetH = Math.max(1, Math.round(height * scale));

      const canvas = document.createElement("canvas");
      canvas.width = targetW;
      canvas.height = targetH;
      const ctx = canvas.getContext("2d");
      if (!ctx) {
        if (isStandardDirectImage) return file;
        throw new Error("Canvas 2D context unavailable.");
      }
      ctx.drawImage(decoded.source, 0, 0, targetW, targetH);

      const blob = await new Promise((resolve, reject) => {
        canvas.toBlob(
          (b) => (b ? resolve(b) : reject(new Error("Failed to encode compressed JPEG."))),
          "image/jpeg",
          quality
        );
      });
      return blob;
    } catch (encodeErr) {
      if (isStandardDirectImage) {
        return file;
      }
      throw encodeErr;
    } finally {
      if (decoded && typeof decoded.cleanup === "function") {
        decoded.cleanup();
      }
    }
  }

  async function fetchWithTimeout(url, options, timeoutMs) {
    const ms = Number(timeoutMs || 35000);
    const controller = typeof AbortController === "function" ? new AbortController() : null;
    const opts = options ? { ...options } : {};
    let timer = null;

    if (controller) {
      const externalSignal = opts.signal;
      if (externalSignal && externalSignal.aborted) {
        controller.abort();
      } else if (externalSignal && typeof externalSignal.addEventListener === "function") {
        externalSignal.addEventListener("abort", () => controller.abort(), { once: true });
      }
      opts.signal = controller.signal;
      timer = setTimeout(() => controller.abort(), ms);
    }

    try {
      return await fetch(url, opts);
    } catch (err) {
      if (err && err.name === "AbortError") {
        throw new Error("Photo upload timed out. Please check your connection and retry.");
      }
      throw err;
    } finally {
      if (timer) clearTimeout(timer);
    }
  }

  window.ClassPulseImage = {
    compressToJpeg,
    fetchWithTimeout,
  };
})();
