"use strict";
// Which run the headline card shows: the latest gate run of this PR.
const HEADLINE = { app: "sql-chat", pr: 2 };
const NS = "http://www.w3.org/2000/svg";

const pct = (x) => (x * 100).toFixed(1).replace(/\.0$/, "") + "%";
const day = (iso) => iso.slice(0, 10);
const fmtP = (p) => (p < 0.001 ? "<0.001" : p.toFixed(p < 0.01 ? 4 : 3));

function el(tag, attrs, children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k === "text") node.textContent = v;
    else node.setAttribute(k, v);
  }
  for (const c of children || []) node.append(c);
  return node;
}

function svg(tag, attrs, children) {
  const node = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k === "text") node.textContent = v;
    else node.setAttribute(k, String(v));
  }
  for (const c of children || []) node.append(c);
  return node;
}

function verdictBadge(v) {
  const icon = svg("svg", { viewBox: "0 0 10 10", "aria-hidden": "true" }, [
    v === "FAIL"
      ? svg("path", { d: "M5 0.5 9.5 5 5 9.5 0.5 5z", fill: "currentColor" })
      : svg("circle", { cx: 5, cy: 5, r: 4.5, fill: "currentColor" }),
  ]);
  const badge = el("span", { class: "verdict " + v });
  badge.append(icon, v);
  return badge;
}

function describe(r) {
  return `${day(r.created_at)}, PR #${r.pr} ${r.pr_title}: base ${r.base_correct}/${r.n} (${pct(
    r.base_correct / r.n
  )}), PR ${r.head_correct}/${r.n} (${pct(r.head_correct / r.n)}), b=${r.b} c=${r.c}, p=${fmtP(
    r.p_value
  )}, ${r.verdict}`;
}

function prUrl(r) {
  return `https://github.com/${r.repo}/pull/${r.pr}`;
}

function renderHeadline(runs) {
  const mine = runs.filter((r) => r.app === HEADLINE.app && r.pr === HEADLINE.pr);
  if (!mine.length) return;
  const r = mine[mine.length - 1];
  const stateLabel = r.pr_state === "open" ? "still open" : r.pr_state;
  const dl = el("dl", { class: "nums" }, [
    el("div", {}, [el("dt", { text: "Base" }), el("dd", { text: pct(r.base_correct / r.n) }, [])]),
    el("div", {}, [el("dt", { text: "This PR" }), el("dd", { text: pct(r.head_correct / r.n) })]),
    el("div", {}, [el("dt", { text: "Cases" }), el("dd", { text: `${r.base_correct} to ${r.head_correct}` })]),
    el("div", {}, [el("dt", { text: "Flips b / c" }), el("dd", { text: `${r.b} / ${r.c}` })]),
    el("div", {}, [el("dt", { text: "p-value" }), el("dd", { text: fmtP(r.p_value) })]),
  ]);
  const title = el("h3", {}, [
    el("a", { href: prUrl(r), text: `PR #${r.pr}: ${r.pr_title}` }),
  ]);
  const card = document.getElementById("headline-card");
  card.className = "card " + r.verdict;
  card.append(
    title,
    el("p", { class: "sub", text: `${r.repo}, branch ${r.branch}, ${stateLabel}. Run on ${day(r.created_at)} with ${r.n} cases.` }),
    dl
  );
  const line = el("p");
  line.append(verdictBadge(r.verdict), ` The gate ${r.verdict === "FAIL" ? "fails" : "passes"} it: ${r.b} cases went from right to wrong and ${r.c} the other way. `);
  card.append(line);
  card.append(
    el("p", {
      text: "The change removes the notes saying only completed orders count as revenue. The unit tests stay green; only the eval set notices.",
    }),
    el("p", {}, [el("a", { href: r.run_url, text: "Open the gate run" }), " · ", el("a", { href: prUrl(r), text: "Open the pull request" })])
  );
  document.getElementById("headline").hidden = false;
}

function chart(runs, readout) {
  const W = Math.max(560, runs.length * 44 + 70);
  const H = 250, top = 14, bottom = 38, left = 44, right = 12;
  const slot = (W - left - right) / runs.length;
  const y = (v) => top + (1 - v) * (H - top - bottom);
  const root = svg("svg", { viewBox: `0 0 ${W} ${H}`, role: "group", "aria-label": "Accuracy of the base and of each pull request, per gate run" });
  for (const v of [0, 0.25, 0.5, 0.75, 1]) {
    root.append(svg("line", { class: "grid", x1: left, x2: W - right, y1: y(v), y2: y(v) }));
    root.append(svg("text", { x: left - 6, y: y(v) + 4, "text-anchor": "end", text: pct(v) }));
  }
  let lastLabel = -1;
  runs.forEach((r, i) => {
    const cx = left + slot * (i + 0.5);
    const dx = Math.min(6, slot / 5);
    const g = svg("g", { class: "run", tabindex: 0, role: "img", "aria-label": describe(r) });
    g.append(svg("title", { text: describe(r) }));
    if (r.verdict === "FAIL") {
      g.append(svg("rect", { class: "band", x: cx - slot / 2, y: top, width: slot, height: H - top - bottom }));
    }
    g.append(svg("rect", { class: "hit", x: cx - slot / 2, y: top, width: slot, height: H - top - bottom }));
    const base = r.base_correct / r.n, head = r.head_correct / r.n;
    if (r.base_ci.length === 2) g.append(svg("line", { class: "ci-base", x1: cx - dx, x2: cx - dx, y1: y(r.base_ci[0]), y2: y(r.base_ci[1]) }));
    if (r.head_ci.length === 2) g.append(svg("line", { class: "ci-pr", x1: cx + dx, x2: cx + dx, y1: y(r.head_ci[0]), y2: y(r.head_ci[1]) }));
    g.append(svg("circle", { class: "pt-base", cx: cx - dx, cy: y(base), r: 3.5 }));
    if (r.verdict === "FAIL") {
      const s = 5, px = cx + dx, py = y(head);
      g.append(svg("path", { class: "pt-pr FAIL", d: `M${px} ${py - s - 1}L${px + s + 1} ${py}L${px} ${py + s + 1}L${px - s - 1} ${py}z` }));
    } else {
      g.append(svg("circle", { class: "pt-pr", cx: cx + dx, cy: y(head), r: 3.5 }));
    }
    const show = () => { readout.textContent = describe(r); };
    g.addEventListener("mouseenter", show);
    g.addEventListener("focus", show);
    root.append(g);
    if (i === 0 || i - lastLabel >= Math.ceil(70 / slot) || r.verdict === "FAIL") {
      if (i - lastLabel >= 2 || lastLabel < 0) {
        root.append(svg("text", { x: cx, y: H - 20, "text-anchor": "middle", text: day(r.created_at).slice(5) }));
        lastLabel = i;
      }
    }
  });
  root.append(svg("text", { x: left, y: H - 4, text: "gate runs, oldest first, one slot per run" }));
  return root;
}

function legend() {
  const item = (cls, shape, label) => {
    const s = svg("svg", { viewBox: "0 0 16 16", "aria-hidden": "true" }, [shape]);
    const li = el("li");
    li.append(s, label);
    return li;
  };
  const ul = el("ul", { class: "legend" });
  ul.append(
    item("l-base", svg("circle", { class: "l-base", cx: 8, cy: 8, r: 4.5 }), "base accuracy"),
    item("l-pr", svg("circle", { class: "l-pr", cx: 8, cy: 8, r: 5 }), "PR accuracy, passed"),
    item("l-fail", svg("path", { class: "l-fail", d: "M8 1.5 14.5 8 8 14.5 1.5 8z" }), "PR accuracy, failed (shaded)"),
    el("li", { text: "bars: 95% interval" })
  );
  return ul;
}

function table(runs, repo) {
  const head = el("tr", {}, ["Date", "PR", "Base", "PR", "b / c", "p", "Verdict", "Run"].map((h, i) =>
    el("th", { scope: "col", class: i >= 2 && i <= 5 ? "num" : "", text: h })
  ));
  const body = el("tbody");
  for (const r of [...runs].reverse()) {
    const prCell = el("td", { class: "pr" });
    prCell.append(el("a", { href: prUrl(r), text: `#${r.pr}` }), " " + r.pr_title, " ");
    prCell.append(el("span", { class: "state", text: `(${r.pr_state})` }));
    const tr = el("tr", { class: r.verdict === "FAIL" ? "fail" : "" }, [
      el("td", { class: "date", text: day(r.created_at) }),
      prCell,
      el("td", { class: "num", text: `${r.base_correct}/${r.n}` }),
      el("td", { class: "num", text: `${r.head_correct}/${r.n}` }),
      el("td", { class: "num", text: `${r.b} / ${r.c}` }),
      el("td", { class: "num", text: fmtP(r.p_value) }),
      el("td", {}, [verdictBadge(r.verdict)]),
      el("td", {}, [el("a", { href: r.run_url, text: "run" })]),
    ]);
    body.append(tr);
  }
  return el("div", { class: "tablewrap" }, [
    el("table", {}, [el("caption", { text: `Gate runs for ${repo}, newest first` }), el("thead", {}, [head]), body]),
  ]);
}

function renderApps(runs) {
  const apps = [];
  for (const r of runs) if (!apps.includes(r.app)) apps.push(r.app);
  const host = document.getElementById("apps");
  for (const app of apps) {
    const mine = runs.filter((r) => r.app === app);
    const repo = mine[0].repo;
    const fails = mine.filter((r) => r.verdict === "FAIL").length;
    const sec = el("section", { "aria-labelledby": "h-" + app });
    const readout = el("p", { class: "readout", "aria-live": "polite", text: "Hover or focus a run to read its numbers." });
    sec.append(
      el("h2", { id: "h-" + app, text: repo.split("/")[1] }),
      el("p", { class: "app-meta", text: `${mine.length} gate runs on ${mine[0].n} cases, ${fails} failed. ` }),
    );
    sec.querySelector(".app-meta").append(el("a", { href: "https://github.com/" + repo, text: repo }));
    sec.append(el("div", { class: "chart" }, [chart(mine, readout)]), legend(), readout, table(mine, repo));
    host.append(sec);
  }
}

fetch("data/runs.json")
  .then((res) => { if (!res.ok) throw new Error(res.status); return res.json(); })
  .then((data) => {
    const runs = (data.runs || []).slice().sort((a, b) => a.created_at.localeCompare(b.created_at));
    renderHeadline(runs);
    renderApps(runs);
  })
  .catch(() => { document.getElementById("load-error").hidden = false; });
