/* Follow the Home Assistant theme.
 *
 * Ported from Ethan Medeiros's fork (threethan/petlibro-local-camera, MIT),
 * trimmed to the colors this UI uses.
 *
 * Ingress shows this page in an iframe, so HA's CSS variables don't reach it.
 * The ingress URL is on the same origin as HA, so we can read the theme from
 * the parent page and copy it into our own --pl-* tokens. Opened outside HA
 * (or if reading the parent fails), the built-in palette in app.css stays.
 */
(() => {
  // Our token -> HA variables to try, in order.
  const tokenMap = {
    "--pl-bg":          ["--primary-background-color"],
    "--pl-surface":     ["--card-background-color", "--ha-card-background"],
    "--pl-surface-2":   ["--secondary-background-color"],
    "--pl-border":      ["--divider-color"],
    "--pl-accent":      ["--primary-color"],
    "--pl-accent-dim":  ["--dark-primary-color", "--primary-color"],
    "--pl-text":        ["--primary-text-color"],
    "--pl-subtext":     ["--secondary-text-color"],
    "--pl-danger":      ["--error-color"],
    "--pl-success":     ["--success-color"],
    "--pl-warning":     ["--warning-color"],
    "--primary-font-family": ["--primary-font-family"],
  };

  const hostDocument = () => {
    try {
      return window.parent !== window ? window.parent.document : null;
    } catch {
      return null;  // cross-origin: not inside HA ingress
    }
  };

  // HA sets theme variables on <html>; some versions also on <home-assistant>.
  const themedElements = (doc) =>
    [doc.querySelector("home-assistant"), doc.documentElement].filter(Boolean);

  const read = (elements, names) => {
    for (const name of names) {
      for (const el of elements) {
        const v = getComputedStyle(el).getPropertyValue(name).trim();
        // Computed custom properties normally have var() already resolved; skip
        // anything that still doesn't, rather than copying a broken reference.
        if (v && !v.includes("var(")) return v;
      }
    }
    return "";
  };

  // Light or dark, from the background's brightness, so the light-mode
  // shadow and other light rules in app.css follow HA instead of the OS.
  const isLight = (color) => {
    const ctx = document.createElement("canvas").getContext("2d");
    if (!ctx) return null;
    ctx.fillStyle = "#000";
    ctx.fillStyle = color;
    ctx.fillRect(0, 0, 1, 1);
    const [r, g, b, a] = ctx.getImageData(0, 0, 1, 1).data;
    if (!a) return null;
    return (0.299 * r + 0.587 * g + 0.114 * b) > 140;
  };

  const apply = () => {
    const doc = hostDocument();
    if (!doc) return;
    const elements = themedElements(doc);
    const root = document.documentElement;
    let applied = false;
    for (const [local, names] of Object.entries(tokenMap)) {
      const v = read(elements, names);
      if (v) { root.style.setProperty(local, v); applied = true; }
    }
    if (!applied) return;
    const bg = read(elements, ["--primary-background-color"]);
    const light = bg ? isLight(bg) : null;
    if (light !== null) {
      root.dataset.theme = light ? "light" : "dark";
      root.style.setProperty("--pl-shadow",
        light ? "0 2px 12px rgba(0,0,0,.12)" : "0 2px 12px rgba(0,0,0,.45)");
    }
  };

  let pending = false;
  const schedule = () => {
    if (pending) return;
    pending = true;
    setTimeout(() => { pending = false; apply(); }, 50);
  };

  apply();
  // HA may finish applying its theme just after the ingress page loads.
  [200, 1000, 3000].forEach(ms => setTimeout(apply, ms));

  const doc = hostDocument();
  if (doc && window.MutationObserver) {
    // Theme changes rewrite the style/class attributes on these elements.
    // Watch only those, not HA's whole page.
    const obs = new MutationObserver(schedule);
    for (const el of themedElements(doc)) {
      obs.observe(el, { attributes: true, attributeFilter: ["style", "class"] });
    }
  }
  // "Auto" HA themes switch with the OS dark mode setting.
  window.matchMedia?.("(prefers-color-scheme: dark)").addEventListener?.("change", schedule);
})();
