/* Tiny chart library for the dashboard: plain SVG/HTML strings, no dependencies, colours from the page's CSS variables
   (so light/dark both work). Every function returns an HTML string; user text is escaped. */
const Charts = (() => {
  const esc = s => (s ?? "").toString().replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
  const num = v => (typeof v === "number" && isFinite(v)) ? v : null;
  const f1 = v => num(+v) == null || v == null ? "–" : (+v).toFixed(1);
  const col = v => v == null ? "var(--border)" : (v = Math.round(v * 10) / 10) >= 4 ? "var(--good)" : v >= 3 ? "var(--ok)" : "var(--bad)";   // colour follows the rounded score shown
  const heat = v => v == null ? "" : `background:hsl(${Math.round(Math.max(0, Math.min(1, (v - 1) / 4)) * 125)} 62% 46% / .26)`;
  const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  const mlabel = m => { const [y, mo] = m.split("-"); return `${MONTHS[+mo - 1] || mo} '${y.slice(2)}`; };
  const polar = (cx, cy, r, a) => [cx + r * Math.cos(a), cy + r * Math.sin(a)];

  /** Ring showing a 1-5 score. */
  function gauge(v, {size = 150, label = "Overall"} = {}) {
    const r = (size - 18) / 2, C = 2 * Math.PI * r, frac = v == null ? 0 : Math.max(0, Math.min(1, (v - 1) / 4));
    return `<svg viewBox="0 0 ${size} ${size}" width="${size}" height="${size}" role="img" aria-label="${esc(label)} ${f1(v)} out of 5">
      <circle cx="${size / 2}" cy="${size / 2}" r="${r}" fill="none" stroke="var(--panel-2)" stroke-width="14"/>
      <circle cx="${size / 2}" cy="${size / 2}" r="${r}" fill="none" stroke="${col(v)}" stroke-width="14" stroke-linecap="round"
        stroke-dasharray="${(C * frac).toFixed(1)} ${C.toFixed(1)}" transform="rotate(-90 ${size / 2} ${size / 2})"><title>${esc(label)} ${f1(v)} / 5</title></circle>
      <text x="50%" y="${size / 2 + 2}" text-anchor="middle" class="g-num" style="font-size:${size * 0.27}px;font-weight:700;fill:var(--text)">${f1(v)}</text>
      <text x="50%" y="${size / 2 + size * 0.17}" text-anchor="middle" style="font-size:${size * 0.085}px;fill:var(--muted)">${esc(label)} · of 5</text></svg>`;
  }

  /** parts: [{label, value, color}] */
  function donut(parts, {size = 150, thick = 20, center = "", sub = ""} = {}) {
    const total = parts.reduce((a, p) => a + (p.value || 0), 0), r = (size - thick) / 2, C = 2 * Math.PI * r;
    let off = 0, arcs = "";
    if (total > 0) for (const p of parts) {
      if (!p.value) continue;
      const len = C * p.value / total;
      arcs += `<circle cx="${size / 2}" cy="${size / 2}" r="${r}" fill="none" stroke="${p.color}" stroke-width="${thick}" stroke-dasharray="${len.toFixed(2)} ${(C - len).toFixed(2)}" stroke-dashoffset="${(-off).toFixed(2)}" transform="rotate(-90 ${size / 2} ${size / 2})"><title>${esc(p.label)}: ${p.value} (${Math.round(100 * p.value / total)}%)</title></circle>`;
      off += len;
    }
    return `<svg viewBox="0 0 ${size} ${size}" width="${size}" height="${size}" role="img"><circle cx="${size / 2}" cy="${size / 2}" r="${r}" fill="none" stroke="var(--panel-2)" stroke-width="${thick}"/>${arcs}
      <text x="50%" y="${size / 2 + 4}" text-anchor="middle" style="font-size:${size * 0.2}px;font-weight:700;fill:var(--text)">${esc(total > 0 ? center : "–")}</text>
      ${sub ? `<text x="50%" y="${size / 2 + size * 0.16}" text-anchor="middle" style="font-size:${size * 0.08}px;fill:var(--muted)">${esc(sub)}</text>` : ""}</svg>`;
  }

  function legend(parts, {pct = false} = {}) {
    const total = parts.reduce((a, p) => a + (p.value || 0), 0);
    return `<div class="legend">${parts.map(p => `<div><i style="background:${p.color}"></i><span>${esc(p.label)}</span><b>${p.value ?? 0}${pct && total ? ` <small>${Math.round(100 * (p.value || 0) / total)}%</small>` : ""}</b></div>`).join("")}</div>`;
  }

  /** One 100% bar split into segments. */
  function stack(parts) {
    const total = parts.reduce((a, p) => a + (p.value || 0), 0);
    if (!total) return `<div class="empty small">No data in this period</div>`;
    return `<div class="stack">${parts.filter(p => p.value).map(p => `<span style="width:${100 * p.value / total}%;background:${p.color}" title="${esc(p.label)}: ${p.value} (${Math.round(100 * p.value / total)}%)">${100 * p.value / total >= 9 ? p.value : ""}</span>`).join("")}</div>${legend(parts, {pct: true})}`;
  }

  /** Horizontal bars as HTML rows. items: [{label, value, text, sub, color, key}] */
  function hbars(items, {max, empty = "No data"} = {}) {
    if (!items.length) return `<div class="empty small">${esc(empty)}</div>`;
    const m = max || Math.max(...items.map(i => i.value || 0), 1);
    return `<div class="hbars">${items.map(i => `<div class="hb ${i.key != null ? "click" : ""}" ${i.key != null ? `data-k="${esc(i.key)}"` : ""} title="${esc(i.label)}${i.sub ? " · " + esc(i.sub) : ""}">
      <span class="hb-l">${esc(i.label)}${i.sub ? `<small>${esc(i.sub)}</small>` : ""}</span><div class="hb-t"><i style="width:${Math.max(2, 100 * (i.value || 0) / m)}%;background:${i.color || "var(--accent)"}"></i></div><span class="hb-v">${esc(i.text ?? i.value)}</span></div>`).join("")}</div>`;
  }

  /** Vertical columns. items: [{label, value, color, top, sub}] */
  function columns(items, {max, h = 170, empty = "No data"} = {}) {
    if (!items.length || items.every(i => !i.value && !i.top)) return `<div class="empty small">${esc(empty)}</div>`;
    const m = max || Math.max(...items.map(i => i.value || 0), 1);
    return `<div class="cols" style="height:${h}px">${items.map(i => `<div class="col" title="${esc(i.label)}: ${esc(i.top ?? i.value)}${i.sub ? " · " + esc(i.sub) : ""}">
      <b>${esc(i.top ?? i.value)}</b><div class="col-b"><i style="height:${Math.max(i.value ? 3 : 0, 100 * (i.value || 0) / m)}%;background:${i.color || "var(--accent)"}"></i></div><span>${esc(i.label)}</span>${i.sub ? `<small>${esc(i.sub)}</small>` : ""}</div>`).join("")}</div>`;
  }

  /** PRs per month (bars, left axis) and average score (line, right axis 1-5). months: [{month, prs, overall}] */
  function trend(months, {w = 660, h = 230} = {}) {
    if (!months.length) return `<div class="empty small">No data in this period</div>`;
    const L = 38, R = 34, T = 14, B = 34, iw = w - L - R, ih = h - T - B, maxP = Math.max(...months.map(m => m.prs), 1);
    const step = iw / months.length, bw = Math.min(54, step * 0.6), x = i => L + step * i + step / 2;
    const yP = v => T + ih - (v / maxP) * ih, yS = v => T + ih - ((v - 1) / 4) * ih;
    let g = "";
    for (let s = 1; s <= 5; s++) g += `<line x1="${L}" x2="${w - R}" y1="${yS(s)}" y2="${yS(s)}" stroke="var(--border)" stroke-dasharray="${s === 3 ? "" : "3 4"}" opacity="${s === 3 ? 0.9 : 0.6}"/><text x="${w - R + 6}" y="${yS(s) + 4}" class="ax">${s}</text>`;
    for (let t = 0; t <= 4; t++) { const v = Math.round(maxP * t / 4); g += `<text x="${L - 6}" y="${yP(maxP * t / 4) + 4}" text-anchor="end" class="ax">${v}</text>`; }
    const bars = months.map((m, i) => `<rect x="${x(i) - bw / 2}" y="${yP(m.prs)}" width="${bw}" height="${T + ih - yP(m.prs)}" rx="4" fill="var(--accent-soft)" stroke="var(--accent)" stroke-opacity=".35"><title>${esc(mlabel(m.month))}: ${m.prs} PRs${m.evaluated != null ? `, ${m.evaluated} scored` : ""}</title></rect>
      <text x="${x(i)}" y="${h - B + 16}" text-anchor="middle" class="ax">${esc(mlabel(m.month))}</text>`).join("");
    const pts = months.map((m, i) => m.overall == null ? null : [x(i), yS(m.overall), m]).filter(Boolean);
    const line = pts.length > 1 ? `<polyline fill="none" stroke="var(--text)" stroke-opacity=".55" stroke-width="2" stroke-linejoin="round" points="${pts.map(p => p[0].toFixed(1) + "," + p[1].toFixed(1)).join(" ")}"/>` : "";
    const dots = pts.map(p => `<circle cx="${p[0]}" cy="${p[1]}" r="5.5" fill="${col(p[2].overall)}" stroke="var(--panel)" stroke-width="2"><title>${esc(mlabel(p[2].month))}: average score ${f1(p[2].overall)}</title></circle>`).join("");
    return `<svg viewBox="0 0 ${w} ${h}" width="100%" role="img" aria-label="PRs and average score per month">${g}${bars}${line}${dots}</svg>
      <div class="legend inline"><div><i style="background:var(--accent-soft);outline:1px solid var(--accent)"></i><span>PRs per month (left scale)</span></div><div><i class="dot" style="background:var(--ok)"></i><span>Average score (right scale, 1 to 5)</span></div></div>`;
  }

  /** Spider chart. axes: ["Commits", ...]; series: [{name, values:[1-5|null], color, fill}] */
  function radar(axes, series, {size = 270} = {}) {
    const cx = size / 2, cy = size / 2 + 4, R = size / 2 - 66, n = axes.length, ang = i => -Math.PI / 2 + 2 * Math.PI * i / n;
    const ring = lvl => axes.map((_, i) => polar(cx, cy, R * (lvl - 1) / 4, ang(i)).map(v => v.toFixed(1)).join(",")).join(" ");
    let g = [2, 3, 4, 5].map(l => `<polygon points="${ring(l)}" fill="none" stroke="var(--border)" ${l === 3 ? "" : 'stroke-dasharray="3 4"'}/>`).join("");
    g += axes.map((a, i) => { const [x1, y1] = polar(cx, cy, R, ang(i)), [lx, ly] = polar(cx, cy, R + 22, ang(i));
      return `<line x1="${cx}" y1="${cy}" x2="${x1}" y2="${y1}" stroke="var(--border)"/><text x="${lx}" y="${ly + 4}" text-anchor="${Math.abs(lx - cx) < 8 ? "middle" : lx > cx ? "start" : "end"}" class="ax" style="fill:var(--text);font-size:12px">${esc(a)}</text>`; }).join("");
    const poly = s => axes.map((_, i) => polar(cx, cy, R * (((s.values[i] ?? 1) - 1) / 4), ang(i)).map(v => v.toFixed(1)).join(",")).join(" ");
    const shapes = series.map(s => `<polygon points="${poly(s)}" fill="${s.color}" fill-opacity="${s.fill ?? 0.18}" stroke="${s.color}" stroke-width="2.2" ${s.dash ? 'stroke-dasharray="5 4"' : ""}/>` +
      axes.map((a, i) => s.values[i] == null ? "" : `<circle cx="${polar(cx, cy, R * ((s.values[i] - 1) / 4), ang(i))[0]}" cy="${polar(cx, cy, R * ((s.values[i] - 1) / 4), ang(i))[1]}" r="4" fill="${s.color}"><title>${esc(s.name)} · ${esc(a)}: ${f1(s.values[i])}</title></circle>`).join("")).join("");
    return `<svg viewBox="0 0 ${size} ${size + 8}" width="100%" style="max-width:${size + 40}px" role="img" aria-label="Scores by area">${g}${shapes}</svg>
      <div class="legend inline">${series.map(s => `<div><i style="background:${s.color}"></i><span>${esc(s.name)}</span></div>`).join("")}</div>`;
  }

  function spark(values, {w = 90, h = 26} = {}) {
    const v = values.filter(x => x != null);
    if (v.length < 2) return "";
    const xs = i => 2 + (w - 4) * i / (values.length - 1), ys = x => h - 3 - (h - 6) * ((x - 1) / 4);
    const pts = values.map((x, i) => x == null ? null : xs(i).toFixed(1) + "," + ys(x).toFixed(1)).filter(Boolean).join(" ");
    return `<svg viewBox="0 0 ${w} ${h}" width="${w}" height="${h}"><polyline fill="none" stroke="${col(v[v.length - 1])}" stroke-width="2" points="${pts}"/></svg>`;
  }

  /** Change vs the previous period, as a small coloured chip. */
  function delta(cur, prev, {better = "up", digits = 1, unit = "", vs = "previous period"} = {}) {
    if (cur == null || prev == null) return "";
    const d = cur - prev;
    if (Math.abs(d) < Math.pow(10, -digits) / 2) return `<span class="delta flat" title="same as the ${esc(vs)}">＝ no change</span>`;
    const good = better === "up" ? d > 0 : d < 0;
    return `<span class="delta ${good ? "up" : "down"}" title="vs ${esc(vs)} (${f1(prev)}${unit})">${d > 0 ? "▲" : "▼"} ${d > 0 ? "+" : ""}${d.toFixed(digits)}${unit}</span>`;
  }

  function kpi(value, label, {sub = "", delta: dl = "", tip = "", tone = ""} = {}) {
    return `<div class="kpi ${tone}" title="${esc(tip)}"><div class="v">${value}</div><div class="l">${esc(label)}</div>${sub || dl ? `<div class="s">${dl}${sub ? ` <span>${esc(sub)}</span>` : ""}</div>` : ""}</div>`;
  }

  return {esc, f1, col, heat, mlabel, gauge, donut, legend, stack, hbars, columns, trend, radar, spark, delta, kpi};
})();
