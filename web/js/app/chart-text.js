/* Refinement: keep SVG chart text at a legible rendered size whatever the chart's viewBox scale. */
(function () {
  var TARGET = 11.5, timer = null;
  function fix() {
    var texts = document.querySelectorAll('svg text');
    for (var i = 0; i < texts.length; i++) {
      var t = texts[i], svg = t.ownerSVGElement;
      if (!svg || !svg.getScreenCTM) continue;
      var m = svg.getScreenCTM();
      if (!m || !m.a) continue;
      if (!t.hasAttribute('data-nom')) t.setAttribute('data-nom', parseFloat(getComputedStyle(t).fontSize) || 10);
      var nominal = parseFloat(t.getAttribute('data-nom')), eff = nominal * m.a;
      if (eff < TARGET) {
        t.style.fontSize = (TARGET / m.a).toFixed(2) + 'px';
        t.style.visibility = '';
        try {
          var bb = t.getBBox(), vb = svg.viewBox && svg.viewBox.baseVal;
          if (vb && vb.width && (bb.x < -vb.width * 0.01 || bb.x + bb.width > vb.width * 1.01)) t.style.visibility = 'hidden';
        } catch (e) {}
      } else { t.style.fontSize = ''; t.style.visibility = ''; }
    }
  }
  function later() { clearTimeout(timer); timer = setTimeout(fix, 150); }
  new MutationObserver(function (muts) {
    for (var i = 0; i < muts.length; i++) { var mm = muts[i]; if (mm.type === 'attributes' && mm.target.closest && mm.target.closest('svg')) continue; later(); return; }
  }).observe(document.body, { childList: true, subtree: true, attributes: true, attributeFilter: ['class', 'style', 'hidden'] });
  window.addEventListener('resize', later);
  window.addEventListener('load', later);
  later();
})();
