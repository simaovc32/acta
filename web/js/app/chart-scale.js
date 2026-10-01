// Charts are drawn at the SVG's real pixel size (1 unit = 1px) so text stays a fixed size.
// This re-runs a chart's renderer when its element changes width (window resize, phone rotation).
// The newest renderer wins, so a redraw never uses stale data.
window.actaRedrawOnResize = function (el, fn) {
  if (!el || typeof ResizeObserver === 'undefined') return;
  el._redrawFn = fn;
  if (el._ro) return;
  var w = Math.round(el.getBoundingClientRect().width);
  el._ro = new ResizeObserver(function () {
    var nw = Math.round(el.getBoundingClientRect().width);
    if (nw && Math.abs(nw - w) > 1) { cancelAnimationFrame(el._raf); el._raf = requestAnimationFrame(function () { if (el._redrawFn) el._redrawFn(); }); }
    // Track every width, including 0 (element hidden), so returning to the same real
    // width after being hidden still counts as a change and redraws the chart.
    w = nw;
  });
  el._ro.observe(el);
};

// One HTML tooltip per chart host, placed next to the point being read: centred on it, kept inside the
// host, above the point unless there is no room (then below). x, y are chart pixels (1 unit = 1px).
window.actaTip = function (host, svg) {
  if (getComputedStyle(host).position === 'static') host.style.position = 'relative';
  var tip = host.querySelector(':scope > .tip');
  if (!tip) { tip = document.createElement('div'); tip.className = 'tip'; tip.setAttribute('role', 'status'); host.appendChild(tip); }
  return {
    show: function (html, x, y) {
      tip.innerHTML = html; tip.style.opacity = 1;
      var hr = host.getBoundingClientRect(), sr = svg.getBoundingClientRect();
      var ox = sr.left - hr.left - host.clientLeft, oy = sr.top - hr.top - host.clientTop;
      var tw = tip.offsetWidth, th = tip.offsetHeight, gap = 14;
      var left = Math.max(8, Math.min(host.clientWidth - tw - 8, ox + x - tw / 2));
      var top = oy + y - th - gap;
      if (sr.top + y - th - gap < hr.top + 4) top = oy + y + gap + 4;
      tip.style.left = Math.round(left) + 'px'; tip.style.top = Math.round(top) + 'px';
    },
    hide: function () { tip.style.opacity = 0; }
  };
};
