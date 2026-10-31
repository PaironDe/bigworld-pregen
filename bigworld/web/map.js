/* Karte der Weltgenerierung im Regionsraster. Wird von Koordinator und Worker genutzt.
 * Minecraft-Koordinaten: x nach rechts (Osten), z nach unten (Süden).
 */
(function () {
  const REGION = 512;

  function css(name, fallback) {
    const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return v || fallback;
  }

  class BigWorldMap {
    constructor(canvas, opts = {}) {
      this.canvas = canvas;
      this.ctx = canvas.getContext("2d");
      this.opts = opts;
      this.status = null;
      this.dim = null;
      this.scale = 0.05; // Pixel pro Block
      this.cx = 0; // Blockkoordinate in der Bildmitte
      this.cz = 0;
      this.selectMode = false;
      this.drag = null;
      this.selection = null;
      this.hover = null;
      this.fitted = {};
      this._bind();
      new ResizeObserver(() => this.draw()).observe(canvas);
    }

    setStatus(status) {
      this.status = status;
      if (!this.dim && status.dimensions.length) this.dim = status.dimensions[0].name;
      if (!this.fitted[this.dim]) this.fit();
      this.draw();
    }

    setDim(name) {
      this.dim = name;
      this.selection = null;
      if (!this.fitted[name]) this.fit();
      this.draw();
    }

    setSelectMode(on) {
      this.selectMode = on;
      this.canvas.style.cursor = on ? "crosshair" : "grab";
    }

    setSelection(rect) {
      this.selection = rect;
      this.draw();
    }

    dimInfo() {
      return this.status && this.status.dimensions.find((d) => d.name === this.dim);
    }

    fit() {
      const d = this.dimInfo();
      if (!d) return;
      const n = Math.ceil(d.radius / REGION) * REGION;
      const r = this.canvas.getBoundingClientRect();
      const size = Math.max(64, Math.min(r.width, r.height) - 40);
      this.scale = size / (2 * n);
      this.cx = 0;
      this.cz = 0;
      this.fitted[this.dim] = true;
    }

    toScreen(x, z) {
      const r = this.canvas.getBoundingClientRect();
      return [r.width / 2 + (x - this.cx) * this.scale, r.height / 2 + (z - this.cz) * this.scale];
    }

    toWorld(px, py) {
      const r = this.canvas.getBoundingClientRect();
      return [this.cx + (px - r.width / 2) / this.scale, this.cz + (py - r.height / 2) / this.scale];
    }

    tilesOfDim() {
      return this.status ? this.status.tiles.filter((t) => t.dim === this.dim) : [];
    }

    tileAt(x, z) {
      return this.tilesOfDim().find(
        (t) => x >= t.rx0 * REGION && x < (t.rx1 + 1) * REGION && z >= t.rz0 * REGION && z < (t.rz1 + 1) * REGION
      );
    }

    _bind() {
      const c = this.canvas;
      c.style.cursor = "grab";
      c.addEventListener("wheel", (e) => {
        e.preventDefault();
        const rect = c.getBoundingClientRect();
        const [wx, wz] = this.toWorld(e.clientX - rect.left, e.clientY - rect.top);
        const f = Math.exp(-e.deltaY * 0.0015);
        this.scale = Math.min(4, Math.max(0.002, this.scale * f));
        const [nx, nz] = this.toWorld(e.clientX - rect.left, e.clientY - rect.top);
        this.cx += wx - nx;
        this.cz += wz - nz;
        this.draw();
      }, { passive: false });
      c.addEventListener("pointerdown", (e) => {
        c.setPointerCapture(e.pointerId);
        const rect = c.getBoundingClientRect();
        const px = e.clientX - rect.left, py = e.clientY - rect.top;
        if (this.selectMode) {
          const [x, z] = this.toWorld(px, py);
          this.drag = { mode: "select", x0: Math.round(x), z0: Math.round(z) };
          this.selection = { x0: Math.round(x), z0: Math.round(z), x1: Math.round(x), z1: Math.round(z) };
        } else {
          this.drag = { mode: "pan", px, py, cx: this.cx, cz: this.cz };
          c.style.cursor = "grabbing";
        }
      });
      c.addEventListener("pointermove", (e) => {
        const rect = c.getBoundingClientRect();
        const px = e.clientX - rect.left, py = e.clientY - rect.top;
        const [x, z] = this.toWorld(px, py);
        if (this.drag && this.drag.mode === "pan") {
          this.cx = this.drag.cx - (px - this.drag.px) / this.scale;
          this.cz = this.drag.cz - (py - this.drag.py) / this.scale;
        } else if (this.drag && this.drag.mode === "select") {
          this.selection = { x0: this.drag.x0, z0: this.drag.z0, x1: Math.round(x), z1: Math.round(z) };
        }
        this.hover = { px, py, x: Math.floor(x), z: Math.floor(z), tile: this.tileAt(x, z) };
        this.draw();
        if (this.opts.onHover) this.opts.onHover(this.hover);
      });
      c.addEventListener("pointerleave", () => {
        this.hover = null;
        this.draw();
        if (this.opts.onHover) this.opts.onHover(null);
      });
      c.addEventListener("pointerup", () => {
        if (this.drag && this.drag.mode === "select" && this.opts.onSelect) {
          const s = this.selection;
          this.opts.onSelect({
            x0: Math.min(s.x0, s.x1), z0: Math.min(s.z0, s.z1),
            x1: Math.max(s.x0, s.x1), z1: Math.max(s.z0, s.z1),
          });
        }
        this.drag = null;
        c.style.cursor = this.selectMode ? "crosshair" : "grab";
      });
    }

    draw() {
      const c = this.canvas, ctx = this.ctx;
      const dpr = window.devicePixelRatio || 1;
      const r = c.getBoundingClientRect();
      if (c.width !== Math.round(r.width * dpr) || c.height !== Math.round(r.height * dpr)) {
        c.width = Math.round(r.width * dpr);
        c.height = Math.round(r.height * dpr);
      }
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.fillStyle = css("--map-bg", "#11151a");
      ctx.fillRect(0, 0, r.width, r.height);
      if (!this.status) return;

      const colors = {
        pending: css("--st-pending", "#3a4048"),
        leased: css("--st-leased", "#2f6fd6"),
        merging: css("--st-merging", "#8a5cf6"),
        done: css("--st-done", "#2e9d5b"),
        failed: css("--st-failed", "#d64545"),
      };
      const grid = css("--map-grid", "rgba(255,255,255,0.08)");
      const tileLine = css("--map-tile", "rgba(255,255,255,0.35)");
      const prioColor = css("--st-priority", "#f0a020");
      const text = css("--map-text", "#cfd6de");

      // Kacheln
      for (const t of this.tilesOfDim()) {
        const [x0, y0] = this.toScreen(t.rx0 * REGION, t.rz0 * REGION);
        const [x1, y1] = this.toScreen((t.rx1 + 1) * REGION, (t.rz1 + 1) * REGION);
        const w = x1 - x0, h = y1 - y0;
        if (x1 < 0 || y1 < 0 || x0 > r.width || y0 > r.height) continue;
        ctx.fillStyle = colors.pending;
        ctx.fillRect(x0, y0, w, h);
        if (t.status === "leased") {
          ctx.globalAlpha = 0.35;
          ctx.fillStyle = colors.leased;
          ctx.fillRect(x0, y0, w, h);
          ctx.globalAlpha = 1;
          const p = Math.max(0, Math.min(1, t.progress || 0));
          ctx.fillRect(x0, y1 - h * p, w, h * p);
        } else if (t.status !== "pending") {
          ctx.fillStyle = colors[t.status] || colors.pending;
          ctx.fillRect(x0, y0, w, h);
        }
      }

      // Regionsraster (nur wenn groß genug)
      const regionPx = REGION * this.scale;
      const d = this.dimInfo();
      if (d) {
        const n = Math.ceil(d.radius / REGION);
        if (regionPx >= 6) {
          ctx.strokeStyle = grid;
          ctx.lineWidth = 1;
          ctx.beginPath();
          for (let i = -n; i <= n; i++) {
            const [ax, ay] = this.toScreen(i * REGION, -n * REGION);
            const [bx, by] = this.toScreen(i * REGION, n * REGION);
            ctx.moveTo(Math.round(ax) + 0.5, ay);
            ctx.lineTo(Math.round(bx) + 0.5, by);
            const [cx0, cy0] = this.toScreen(-n * REGION, i * REGION);
            const [cx1, cy1] = this.toScreen(n * REGION, i * REGION);
            ctx.moveTo(cx0, Math.round(cy0) + 0.5);
            ctx.lineTo(cx1, Math.round(cy1) + 0.5);
          }
          ctx.stroke();
        }
      }

      // Kachelränder, Prioritäten, Vergleichskacheln
      for (const t of this.tilesOfDim()) {
        const [x0, y0] = this.toScreen(t.rx0 * REGION, t.rz0 * REGION);
        const [x1, y1] = this.toScreen((t.rx1 + 1) * REGION, (t.rz1 + 1) * REGION);
        ctx.lineWidth = 1;
        ctx.strokeStyle = tileLine;
        ctx.strokeRect(x0 + 0.5, y0 + 0.5, x1 - x0 - 1, y1 - y0 - 1);
        if (t.priority !== null && t.priority !== undefined && t.status !== "done") {
          ctx.lineWidth = 2;
          ctx.strokeStyle = prioColor;
          ctx.strokeRect(x0 + 2, y0 + 2, x1 - x0 - 4, y1 - y0 - 4);
        }
        if (t.verify) {
          ctx.setLineDash([4, 3]);
          ctx.lineWidth = 2;
          ctx.strokeStyle = text;
          ctx.strokeRect(x0 + 5, y0 + 5, x1 - x0 - 10, y1 - y0 - 10);
          ctx.setLineDash([]);
        }
        if (x1 - x0 > 70 && (t.worker || t.status === "leased")) {
          ctx.fillStyle = text;
          ctx.font = "11px system-ui, sans-serif";
          ctx.fillText(`${t.worker || ""} ${Math.round(100 * (t.progress || 0))} %`, x0 + 6, y0 + 16);
        }
      }

      // Prioritätsbereiche
      for (const p of (this.status.priorities || []).filter((p) => p.dim === this.dim)) {
        const [x0, y0] = this.toScreen(p.x0, p.z0);
        const [x1, y1] = this.toScreen(p.x1 + 1, p.z1 + 1);
        ctx.fillStyle = prioColor;
        ctx.globalAlpha = 0.15;
        ctx.fillRect(x0, y0, x1 - x0, y1 - y0);
        ctx.globalAlpha = 1;
        ctx.strokeStyle = prioColor;
        ctx.lineWidth = 1.5;
        ctx.strokeRect(x0, y0, x1 - x0, y1 - y0);
      }

      // Auswahl
      if (this.selection) {
        const s = this.selection;
        const [x0, y0] = this.toScreen(Math.min(s.x0, s.x1), Math.min(s.z0, s.z1));
        const [x1, y1] = this.toScreen(Math.max(s.x0, s.x1) + 1, Math.max(s.z0, s.z1) + 1);
        ctx.setLineDash([6, 4]);
        ctx.strokeStyle = prioColor;
        ctx.lineWidth = 2;
        ctx.strokeRect(x0, y0, x1 - x0, y1 - y0);
        ctx.setLineDash([]);
      }

      // Ursprung 0,0
      const [ox, oy] = this.toScreen(0, 0);
      ctx.strokeStyle = text;
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      ctx.moveTo(ox - 8, oy); ctx.lineTo(ox + 8, oy);
      ctx.moveTo(ox, oy - 8); ctx.lineTo(ox, oy + 8);
      ctx.stroke();

      // Achsenbeschriftung
      this._axes(ctx, r, text);

      // Hover
      if (this.hover) {
        const h = this.hover;
        const lines = [`x ${h.x}, z ${h.z}  ·  Region ${Math.floor(h.x / REGION)}, ${Math.floor(h.z / REGION)}`];
        if (h.tile) {
          const t = h.tile;
          lines.push(`Kachel ${t.tx}, ${t.tz}: ${statusLabel(t.status)}`);
          if (t.worker) lines.push(`auf ${t.worker} (${Math.round(100 * (t.progress || 0))} %)`);
          if (t.done_by) lines.push(`fertig von ${t.done_by}`);
          if (t.priority !== null && t.priority !== undefined) lines.push(`Priorität ${t.priority + 1}`);
          if (t.error) lines.push(`Fehler: ${t.error.slice(0, 60)}`);
        }
        ctx.font = "12px system-ui, sans-serif";
        const w = Math.max(...lines.map((l) => ctx.measureText(l).width)) + 16;
        const bx = Math.min(h.px + 14, r.width - w - 4), by = Math.min(h.py + 14, r.height - lines.length * 16 - 12);
        ctx.fillStyle = css("--tooltip-bg", "rgba(10,12,16,0.92)");
        ctx.fillRect(bx, by, w, lines.length * 16 + 10);
        ctx.fillStyle = css("--tooltip-text", "#e8edf2");
        lines.forEach((l, i) => ctx.fillText(l, bx + 8, by + 18 + i * 16));
      }
    }

    _axes(ctx, r, color) {
      const target = 90 / this.scale; // Blöcke für ~90 px
      const steps = [512, 1024, 2048, 4096, 8192, 16384];
      const step = steps.find((s) => s >= target) || 16384;
      const [wx0, wz0] = this.toWorld(0, 0);
      const [wx1, wz1] = this.toWorld(r.width, r.height);
      ctx.fillStyle = color;
      ctx.font = "11px system-ui, sans-serif";
      for (let x = Math.ceil(wx0 / step) * step; x <= wx1; x += step) {
        const [sx] = this.toScreen(x, 0);
        ctx.fillText(String(x), sx + 3, 12);
      }
      for (let z = Math.ceil(wz0 / step) * step; z <= wz1; z += step) {
        const [, sy] = this.toScreen(0, z);
        ctx.fillText(String(z), 4, sy - 3);
      }
    }
  }

  function statusLabel(s) {
    return { pending: "offen", leased: "in Arbeit", merging: "wird zusammengeführt", done: "fertig", failed: "fehlgeschlagen" }[s] || s;
  }

  function fmtDuration(sec) {
    if (sec === null || sec === undefined) return "–";
    sec = Math.round(sec);
    const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60);
    if (h >= 24) return `${Math.floor(h / 24)} d ${h % 24} h`;
    if (h > 0) return `${h} h ${m} min`;
    return `${m} min`;
  }

  window.BigWorldMap = BigWorldMap;
  window.bwStatusLabel = statusLabel;
  window.bwDuration = fmtDuration;
})();
