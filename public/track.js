// Click tracking for finclator.com: every link and button → POST /panel/api/click (anonymous; a random per-browser
// id in localStorage distinguishes visitors — no cookie). x.com links to a roster account are stored as influencer
// clicks with the handle; everything else as link/button with href + label. Same shape as the panel's beacon.
(() => {
  let v = null;
  try { v = localStorage.fcv || (localStorage.fcv = Math.random().toString(36).slice(2, 12)); } catch {}
  document.addEventListener("click", e => {
    const el = e.target.closest("a,button,summary"); if (!el) return;
    const href = (el.getAttribute && el.getAttribute("href")) || "";
    let h = null;
    if (href.startsWith("https://x.com/")) { const p = href.split("/")[3]; if (p && p !== "status") h = p; }
    const body = JSON.stringify({handle: h, kind: h ? "influencer" : (el.tagName === "A" ? "link" : "button"),
      href: href.slice(0, 200), label: (el.textContent || "").trim().slice(0, 80), src: "site", page: location.pathname, visitor: v});
    try { navigator.sendBeacon("/panel/api/click", new Blob([body], {type: "application/json"})); } catch {}
  });
})();
