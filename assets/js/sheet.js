// Draggable bottom sheet for phones (peek / half / full), side panel on wide screens.

const DESKTOP_QUERY = '(min-width: 900px)';

export function createSheet(el, { grip, getView, onChange }) {
  const desktop = matchMedia(DESKTOP_QUERY);
  let snap = 'half';
  let y = 0;
  let H = 0;
  let points = { full: 0, half: 0, peek: 0 };
  let drag = null;

  const safeBottom = (() => {
    const probe = document.createElement('div');
    probe.style.cssText = 'position:fixed;bottom:0;height:0;padding-bottom:env(safe-area-inset-bottom);visibility:hidden;pointer-events:none';
    document.body.appendChild(probe);
    return () => probe.getBoundingClientRect().height || 0;
  })();

  const scroller = () => getView().querySelector('.sheet-scroll');

  function measure() {
    H = el.offsetHeight;
    const view = getView();
    const head = view.querySelector('.sheet-head');
    const stepper = view.querySelector('.stepper');
    const peekVisible = Math.min(H, grip.offsetHeight + (head ? head.offsetHeight : 0) + (stepper ? stepper.offsetHeight : 0) + safeBottom() + 6);
    const vh = window.innerHeight;
    const halfVisible = Math.min(H, Math.max(peekVisible + 140, Math.round(vh * 0.5)));
    points = { full: 0, half: H - halfVisible, peek: H - peekVisible };
  }

  function apply(animate) {
    if (desktop.matches) {
      el.style.transform = '';
      el.classList.remove('is-dragging');
      notify();
      return;
    }
    el.classList.toggle('is-dragging', !animate);
    el.style.transform = `translate3d(0, ${Math.round(y)}px, 0)`;
    notify();
  }

  function notify() {
    el.dataset.snap = desktop.matches ? 'side' : snap;
    onChange && onChange({ desktop: desktop.matches, snap, visible: desktop.matches ? 0 : Math.max(0, H - y) });
  }

  function setSnap(name, animate = true) {
    snap = name;
    measure();
    y = points[name];
    const sc = scroller();
    if (sc && name === 'peek') sc.scrollTop = 0;
    apply(animate);
  }

  function closest(pos) {
    let best = 'half', d = Infinity;
    for (const [k, v] of Object.entries(points)) {
      if (Math.abs(v - pos) < d) { d = Math.abs(v - pos); best = k; }
    }
    return best;
  }

  // ---- touch dragging -------------------------------------------------------
  el.addEventListener('touchstart', (e) => {
    if (desktop.matches || e.touches.length !== 1) { drag = null; return; }
    const t = e.touches[0];
    const sc = scroller();
    drag = {
      y0: t.clientY, x0: t.clientX, base: y,
      inScroll: Boolean(sc && sc.contains(e.target)),
      decided: false, active: false,
      lastY: t.clientY, lastT: performance.now(), v: 0,
    };
  }, { passive: true });

  el.addEventListener('touchmove', (e) => {
    if (!drag) return;
    const t = e.touches[0];
    const dy = t.clientY - drag.y0;
    const dx = t.clientX - drag.x0;
    if (!drag.decided) {
      if (Math.abs(dy) < 7 && Math.abs(dx) < 7) return;
      drag.decided = true;
      if (Math.abs(dx) > Math.abs(dy)) { drag = null; return; } // horizontal swipe (stepper)
      const sc = scroller();
      if (drag.inScroll && sc) {
        const atTop = sc.scrollTop <= 0;
        if (snap === 'full' && !(atTop && dy > 0)) { drag = null; return; }      // native scroll
        if (snap === 'half' && dy > 0 && !atTop) { drag = null; return; }        // scroll back up first
      }
      if (!e.cancelable) { drag = null; return; }                             // browser already scrolling
      drag.active = true;
      el.classList.add('is-dragging');
    }
    if (!drag.active) return;
    e.preventDefault();
    let ny = drag.base + dy;
    const min = points.full, max = points.peek;
    if (ny < min) ny = min - Math.min(40, (min - ny) * 0.25);
    if (ny > max) ny = max + Math.min(60, (ny - max) * 0.35);
    const now = performance.now();
    const dt = Math.max(1, now - drag.lastT);
    drag.v = 0.8 * ((t.clientY - drag.lastY) / dt) + 0.2 * drag.v;
    drag.lastY = t.clientY; drag.lastT = now;
    y = ny;
    el.style.transform = `translate3d(0, ${Math.round(y)}px, 0)`;
    notify();
  }, { passive: false });

  const end = () => {
    if (!drag) return;
    const wasActive = drag.active;
    const v = drag.v;
    drag = null;
    el.classList.remove('is-dragging');
    if (wasActive) setSnap(pickSnap(y, v));
  };
  el.addEventListener('touchend', end);
  el.addEventListener('touchcancel', end);

  function pickSnap(pos, v) {
    const order = ['full', 'half', 'peek'];
    if (Math.abs(v) > 0.45) {
      // flick: go to the next snap point in the flick direction
      const dir = v > 0 ? 1 : -1;
      const candidates = order.filter((k) => (dir > 0 ? points[k] > pos + 4 : points[k] < pos - 4));
      if (candidates.length) {
        return candidates.reduce((a, b) => (Math.abs(points[a] - pos) < Math.abs(points[b] - pos) ? a : b));
      }
    }
    return closest(pos);
  }

  // ---- mouse dragging on the grip (narrow desktop windows) ------------------
  grip.addEventListener('pointerdown', (e) => {
    if (desktop.matches || e.pointerType !== 'mouse') return;
    const base = y, y0 = e.clientY;
    let moved = false;
    grip.setPointerCapture(e.pointerId);
    const move = (ev) => {
      const dy = ev.clientY - y0;
      if (Math.abs(dy) > 3) moved = true;
      y = Math.min(points.peek, Math.max(points.full, base + dy));
      el.classList.add('is-dragging');
      el.style.transform = `translate3d(0, ${Math.round(y)}px, 0)`;
    };
    const up = () => {
      grip.removeEventListener('pointermove', move);
      grip.removeEventListener('pointerup', up);
      el.classList.remove('is-dragging');
      if (moved) { suppressClick = true; setSnap(closest(y)); }
    };
    grip.addEventListener('pointermove', move);
    grip.addEventListener('pointerup', up);
  });

  // A tap on the grip or on the summary cycles peek → half → full → half.
  let suppressClick = false;
  const onTap = (e) => {
    if (desktop.matches) return;
    if (suppressClick) { suppressClick = false; return; }
    if (e.target.closest('button, a, input, label, select')) return;
    cycle();
  };
  grip.addEventListener('click', onTap);
  el.addEventListener('click', (e) => { if (e.target.closest('.sheet-head')) onTap(e); });

  function cycle() {
    setSnap(snap === 'peek' ? 'half' : snap === 'half' ? 'full' : 'half');
  }

  const relayout = () => setSnap(snap, false);
  desktop.addEventListener('change', relayout);
  window.addEventListener('resize', relayout);
  if (window.visualViewport) window.visualViewport.addEventListener('resize', relayout);
  window.addEventListener('orientationchange', () => setTimeout(relayout, 250));

  return {
    setSnap,
    getSnap: () => (desktop.matches ? 'side' : snap),
    isDesktop: () => desktop.matches,
    visible: () => (desktop.matches ? 0 : Math.max(0, H - y)),
    refresh: relayout,
    expandAtLeast(name) {
      const rank = { peek: 0, half: 1, full: 2 };
      if (!desktop.matches && rank[snap] < rank[name]) setSnap(name);
    },
  };
}
