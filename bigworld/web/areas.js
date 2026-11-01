/* Bereiche anlegen, ordnen und löschen. Wird von Koordinator- und Worker-Oberfläche genutzt.
 * call(method, path, body) muss die Koordinator-API /api/v1/... aufrufen und JSON liefern.
 */
(function () {
  const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const num = (v) => (v === "" || v === null || v === undefined ? NaN : Number(v));
  const fmt = (n) => Math.round(n).toLocaleString("de");
  const dimShort = (d) => d.replace("minecraft:", "").replace("the_", "");

  class AreaPanel {
    constructor(root, map, call) {
      this.root = root;
      this.map = map;
      this.call = call;
      this.status = null;
      this.mode = "radius";
      root.innerHTML = `
        <div class="tabs">
          <button data-mode="radius" class="active">Mitte + Radius</button>
          <button data-mode="rect">Rechteck</button>
        </div>
        <div class="coords" data-for="radius">
          <label>Mitte x<input type="number" name="cx" value="0"></label>
          <label>Mitte z<input type="number" name="cz" value="0"></label>
          <label>Radius (Blöcke)<input type="number" name="r" value="2048" min="16"></label>
          <span></span>
        </div>
        <div class="coords hidden" data-for="rect">
          <label>x von<input type="number" name="x0"></label>
          <label>z von<input type="number" name="z0"></label>
          <label>x bis<input type="number" name="x1"></label>
          <label>z bis<input type="number" name="z1"></label>
        </div>
        <label>Bezeichnung (optional)<input name="label" placeholder="z. B. Spawn, Bergkette Nord"></label>
        <div class="row">
          <button name="draw">Auf Karte aufziehen</button>
          <button name="add" class="primary">Hinzufügen</button>
          <button name="front">Hinzufügen und vorziehen</button>
        </div>
        <div class="muted" name="preview">Bereiche werden der Reihe nach generiert, jeweils von der Mitte nach außen.</div>
        <div class="err" name="err"></div>
        <div name="list"></div>`;
      this.$ = (sel) => root.querySelector(sel);
      root.querySelectorAll("[data-mode]").forEach((b) => (b.onclick = () => this.setMode(b.dataset.mode)));
      root.querySelectorAll("input[type=number]").forEach((i) => i.addEventListener("input", () => this.preview()));
      this.$("[name=draw]").onclick = () => {
        this.map.setSelectMode(!this.map.selectMode);
        this.$("[name=draw]").classList.toggle("active", this.map.selectMode);
      };
      this.$("[name=add]").onclick = () => this.add(false);
      this.$("[name=front]").onclick = () => this.add(true);
      this.$("[name=list]").addEventListener("click", (e) => {
        const b = e.target.closest("button[data-act]");
        if (b) this.act(b.dataset.act, Number(b.dataset.id));
      });
    }

    setMode(m) {
      this.mode = m;
      this.root.querySelectorAll("[data-mode]").forEach((b) => b.classList.toggle("active", b.dataset.mode === m));
      this.root.querySelectorAll("[data-for]").forEach((d) => d.classList.toggle("hidden", d.dataset.for !== m));
      this.preview();
    }

    fromMap(sel) {
      // Vom Kartenrechteck übernehmen
      const set = (n, v) => (this.$(`[name=${n}]`).value = v);
      set("x0", sel.x0); set("z0", sel.z0); set("x1", sel.x1); set("z1", sel.z1);
      this.setMode("rect");
      this.map.setSelectMode(false);
      this.$("[name=draw]").classList.remove("active");
    }

    body() {
      const v = (n) => num(this.$(`[name=${n}]`).value);
      let b;
      if (this.mode === "radius") {
        const cx = v("cx"), cz = v("cz"), r = v("r");
        if ([cx, cz, r].some(Number.isNaN) || r < 1) return null;
        b = { x0: cx - r, z0: cz - r, x1: cx + r - 1, z1: cz + r - 1 };
      } else {
        b = { x0: v("x0"), z0: v("z0"), x1: v("x1"), z1: v("z1") };
        if ([b.x0, b.z0, b.x1, b.z1].some(Number.isNaN)) return null;
      }
      return { dim: this.map.dim, ...b, label: this.$("[name=label]").value.trim() };
    }

    async preview() {
      const b = this.body();
      const out = this.$("[name=preview]");
      if (!b || !b.dim) { this.map.setSelection(null); return; }
      this.map.setSelection(b);
      try {
        const p = await this.call("POST", "/api/v1/areas/preview", b);
        const cps = this.status ? this.status.totals.cps : 0;
        const time = cps > 0 ? ` · bei ${cps.toFixed(0)} Chunks/s etwa ${bwDuration(p.new_chunks / cps)}` : "";
        out.textContent = `${dimShort(b.dim)}: x ${p.x0}…${p.x1}, z ${p.z0}…${p.z1} (auf Regionen gerundet) · ` +
          `${fmt(p.new_chunks)} neue Chunks${p.new_regions < p.regions ? `, ${fmt((p.regions - p.new_regions) * 1024)} schon fertig oder geplant` : ""}${time}`;
        this.$("[name=err]").textContent = "";
      } catch (e) { out.textContent = ""; this.$("[name=err]").textContent = e.message; }
    }

    async add(front) {
      const b = this.body();
      this.$("[name=err]").textContent = "";
      if (!b) { this.$("[name=err]").textContent = "Bitte die Koordinaten vollständig angeben."; return; }
      try {
        await this.call("POST", "/api/v1/areas", { ...b, front });
        this.map.setSelection(null);
        this.$("[name=label]").value = "";
        if (this.onChange) this.onChange();
      } catch (e) { this.$("[name=err]").textContent = e.message; }
    }

    async act(act, id) {
      const ids = this.status.areas.map((a) => a.id);
      const i = ids.indexOf(id);
      try {
        if (act === "del") {
          const a = this.status.areas[i];
          if (!confirm(`Bereich "${a.label || `x ${a.x0}…${a.x1}, z ${a.z0}…${a.z1}`}" entfernen?\nFertig generierte Kacheln bleiben erhalten, offene werden nicht mehr generiert.`)) return;
          await this.call("DELETE", `/api/v1/areas/${id}`);
        } else {
          const j = act === "top" ? 0 : i + (act === "up" ? -1 : 1);
          if (j < 0 || j >= ids.length) return;
          ids.splice(i, 1);
          ids.splice(j, 0, id);
          await this.call("POST", "/api/v1/areas/reorder", { ids });
        }
        if (this.onChange) this.onChange();
      } catch (e) { this.$("[name=err]").textContent = e.message; }
    }

    render(status) {
      this.status = status;
      const areas = status.areas || [];
      this.$("[name=list]").innerHTML = areas.length
        ? `<table><thead><tr><th>#</th><th>Bereich</th><th>Fortschritt</th><th></th></tr></thead><tbody>${areas.map((a, i) => {
            const pct = Math.round(100 * a.progress);
            return `<tr>
              <td>${i + 1}</td>
              <td>${esc(a.label || "Bereich")} <span class="muted">${dimShort(a.dim)}</span>
                <div class="muted" style="font-size:12px">x ${a.x0}…${a.x1}, z ${a.z0}…${a.z1} · ${fmt(a.chunks_total)} Chunks${a.created_by ? ` · von ${esc(a.created_by)}` : ""}</div></td>
              <td style="min-width:90px">${pct} %<div class="bar"><div style="width:${pct}%"></div></div></td>
              <td class="row" style="flex-wrap:nowrap">
                <button data-act="top" data-id="${a.id}" title="ganz nach vorne">⤒</button>
                <button data-act="up" data-id="${a.id}" title="nach oben">↑</button>
                <button data-act="down" data-id="${a.id}" title="nach unten">↓</button>
                <button data-act="del" data-id="${a.id}" class="danger" title="entfernen">✕</button></td></tr>`;
          }).join("")}</tbody></table>`
        : `<div class="muted" style="margin-top:8px">Noch kein Bereich. Lege oben einen an, zum Beispiel Mitte 0, 0 mit Radius 2048.</div>`;
    }
  }

  window.AreaPanel = AreaPanel;
})();
