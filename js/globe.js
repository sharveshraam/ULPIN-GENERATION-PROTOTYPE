/**
 * Rotating background Earth for the landing page.
 *
 * Renders a real textured globe (NASA-derived imagery via CDN) lit by a
 * directional "sun", wrapped in an atmospheric rim shell, with pulsing
 * markers at real Indian cities.
 *
 * Behaviours:
 *   - Spins continuously; scrolling spins it FASTER, then it eases back to
 *     its idle speed. Scroll direction sets the spin direction.
 *   - Purely decorative: pointer-events none, aria-hidden, always behind
 *     content. It can never intercept a click.
 *   - Degrades in stages, never breaking the page:
 *       no WebGL / no three.js -> does nothing, CSS aurora remains
 *       texture CDN blocked    -> falls back to a wireframe + point-cloud
 *                                 globe that still reads as Earth
 *   - prefers-reduced-motion   -> one static frame, no animation loop
 *   - Pauses rAF when the tab is hidden or the globe scrolls out of view.
 *
 * three.js r0.128.0 UMD globals. Do NOT upgrade without switching to an
 * importmap: r148+ removed examples/js/, r150+ removed build/three.min.js.
 */
const Globe = (() => {
  let renderer, scene, camera, root, earth, frame = null, host = null;
  let visible = true, reduced = false, started = false;

  // Idle spin, plus the scroll-driven boost that decays back to idle.
  const IDLE_SPIN = 0.0016;
  let spinBoost = 0;
  let lastScrollY = 0;

  const RADIUS = 1;

  // Texture candidates, tried in order. All are standard three-globe assets.
  // earth-dark suits the dark UI; blue-marble is the photographic fallback.
  const TEXTURES = [
    'https://cdn.jsdelivr.net/npm/three-globe@2.24.10/example/img/earth-dark.jpg',
    'https://cdn.jsdelivr.net/npm/three-globe@2.24.10/example/img/earth-blue-marble.jpg',
    'https://unpkg.com/three-globe@2.24.10/example/img/earth-dark.jpg',
  ];

  const CITIES = [
    { name: 'Delhi',     lat: 28.6139, lon: 77.2090 },
    { name: 'Mumbai',    lat: 19.0760, lon: 72.8777 },
    { name: 'Kolkata',   lat: 22.5726, lon: 88.3639 },
    { name: 'Chennai',   lat: 13.0827, lon: 80.2707 },
    { name: 'Bengaluru', lat: 12.9716, lon: 77.5946 },
    { name: 'Kochi',     lat:  9.9312, lon: 76.2673 },
    { name: 'Hyderabad', lat: 17.3850, lon: 78.4867 },
    { name: 'Ahmedabad', lat: 23.0225, lon: 72.5714 },
  ];

  /** Lat/lon (degrees) -> point on a sphere. Matches three.js UV orientation. */
  function toVector(lat, lon, radius = RADIUS) {
    const phi = (90 - lat) * Math.PI / 180;
    const theta = (lon + 180) * Math.PI / 180;
    return new THREE.Vector3(
      -radius * Math.sin(phi) * Math.cos(theta),
       radius * Math.cos(phi),
       radius * Math.sin(phi) * Math.sin(theta)
    );
  }

  /** Evenly spread points over a sphere (Fibonacci lattice) — fallback skin. */
  function pointCloud(count) {
    const positions = new Float32Array(count * 3);
    const golden = Math.PI * (3 - Math.sqrt(5));
    for (let i = 0; i < count; i++) {
      const y = 1 - (i / (count - 1)) * 2;
      const r = Math.sqrt(Math.max(0, 1 - y * y));
      const t = golden * i;
      positions[i * 3]     = Math.cos(t) * r * RADIUS;
      positions[i * 3 + 1] = y * RADIUS;
      positions[i * 3 + 2] = Math.sin(t) * r * RADIUS;
    }
    const geo = new THREE.BufferGeometry();
    geo.setAttribute('position', new THREE.BufferAttribute(positions, 3));
    return new THREE.Points(geo, new THREE.PointsMaterial({
      color: 0x2dd4bf, size: 0.011, transparent: true, opacity: 0.55,
      sizeAttenuation: true, depthWrite: false,
    }));
  }

  /**
   * Try each texture URL in turn; resolve with the first that loads, or null
   * if every one fails (offline, CDN blocked, corporate proxy...).
   */
  function loadFirstTexture(urls) {
    return new Promise((resolve) => {
      const loader = new THREE.TextureLoader();
      loader.setCrossOrigin('anonymous');
      let i = 0;
      const attempt = () => {
        if (i >= urls.length) { resolve(null); return; }
        const url = urls[i++];
        loader.load(url, (tex) => resolve(tex), undefined, () => attempt());
      };
      attempt();
    });
  }

  function build() {
    root = new THREE.Group();

    // --- Earth sphere. Starts as a flat dark ball; the texture is swapped in
    //     when (and if) it arrives, so the globe is visible immediately. ---
    earth = new THREE.Mesh(
      new THREE.SphereGeometry(RADIUS, 64, 48),
      new THREE.MeshPhongMaterial({
        color: 0x0d2a38, emissive: 0x04141d, specular: 0x0b3b47,
        shininess: 12,
      })
    );
    root.add(earth);

    // Faint graticule, so it reads as a data globe rather than a photo.
    root.add(new THREE.LineSegments(
      new THREE.WireframeGeometry(new THREE.SphereGeometry(RADIUS * 1.002, 36, 24)),
      new THREE.LineBasicMaterial({ color: 0x14b8a6, transparent: true, opacity: 0.07, depthWrite: false })
    ));

    // Atmosphere: a back-side shell fakes a rim glow without post-processing.
    root.add(new THREE.Mesh(
      new THREE.SphereGeometry(RADIUS * 1.06, 48, 32),
      new THREE.MeshBasicMaterial({
        color: 0x22d3ee, transparent: true, opacity: 0.07,
        side: THREE.BackSide, depthWrite: false,
      })
    ));

    // City markers.
    const markers = new THREE.Group();
    CITIES.forEach(c => {
      const p = toVector(c.lat, c.lon, RADIUS * 1.008);
      const dot = new THREE.Mesh(
        new THREE.SphereGeometry(0.013, 10, 10),
        new THREE.MeshBasicMaterial({ color: 0x5eead4 })
      );
      dot.position.copy(p);
      markers.add(dot);

      const halo = new THREE.Mesh(
        new THREE.SphereGeometry(0.029, 12, 12),
        new THREE.MeshBasicMaterial({ color: 0x2dd4bf, transparent: true, opacity: 0.3, depthWrite: false })
      );
      halo.position.copy(p);
      halo.userData.phase = Math.random() * Math.PI * 2;
      markers.add(halo);
    });
    root.userData.markers = markers;
    root.add(markers);

    // Axial tilt, and start rotated so India faces the camera. -2.932 rad is
    // the solved angle that maximises the +z component for ~20N 78E; every
    // marker city sits on the visible hemisphere there.
    root.rotation.z = 0.36;
    root.rotation.y = -2.932;
    scene.add(root);

    // Lighting: a key "sun" plus enough ambient that the night side is not
    // a black void against the dark page.
    const sun = new THREE.DirectionalLight(0xffffff, 1.15);
    sun.position.set(-2.2, 1.4, 2.4);
    scene.add(sun);
    scene.add(new THREE.AmbientLight(0x93c5fd, 0.55));

    // Swap in the real Earth texture once it loads; otherwise add the
    // point-cloud skin so the sphere still has surface detail.
    loadFirstTexture(TEXTURES).then((tex) => {
      if (tex) {
        earth.material.map = tex;
        earth.material.color = new THREE.Color(0xffffff);
        earth.material.emissive = new THREE.Color(0x0a1a22);
        earth.material.needsUpdate = true;
      } else {
        root.add(pointCloud(1400));
      }
      if (reduced) render(0);   // static mode: redraw the one frame
    });
  }

  function resize() {
    if (!host || !renderer) return;
    const w = host.clientWidth || 1, h = host.clientHeight || 1;
    renderer.setSize(w, h, false);
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
  }

  function render(t) {
    const markers = root.userData.markers;
    if (markers) {
      markers.children.forEach(m => {
        if (m.userData.phase === undefined) return;
        const pulse = 1 + Math.sin(t * 0.0022 + m.userData.phase) * 0.42;
        m.scale.setScalar(pulse);
        m.material.opacity = 0.34 - (pulse - 1) * 0.2;
      });
    }
    renderer.render(scene, camera);
  }

  function loop(t) {
    if (!visible) { frame = null; return; }
    root.rotation.y += IDLE_SPIN + spinBoost;
    spinBoost *= 0.94;                       // ease back to the idle speed
    if (Math.abs(spinBoost) < 1e-6) spinBoost = 0;
    render(t);
    frame = requestAnimationFrame(loop);
  }

  function start() {
    if (frame === null && visible && !reduced) frame = requestAnimationFrame(loop);
  }
  function stop() {
    if (frame !== null) { cancelAnimationFrame(frame); frame = null; }
  }

  /** Scrolling spins the globe faster; direction follows the scroll. */
  function bindScroll() {
    lastScrollY = window.scrollY || 0;
    window.addEventListener('scroll', () => {
      const y = window.scrollY || 0;
      const dy = y - lastScrollY;
      lastScrollY = y;
      // Scale into a sane range and clamp, so a flung trackpad cannot make
      // the globe strobe.
      spinBoost += dy * 0.00022;
      spinBoost = Math.max(-0.075, Math.min(0.075, spinBoost));
      if (frame === null) start();          // resume if it had idled out
    }, { passive: true });
  }

  function init(hostId = 'globe') {
    host = document.getElementById(hostId);
    if (!host) return false;
    if (typeof THREE === 'undefined') return false;   // CDN blocked

    try {
      renderer = new THREE.WebGLRenderer({ alpha: true, antialias: true });
    } catch (_) {
      return false;                                   // no WebGL
    }
    if (!renderer || !renderer.getContext || !renderer.getContext()) return false;

    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    renderer.setClearColor(0x000000, 0);
    host.appendChild(renderer.domElement);

    scene = new THREE.Scene();
    camera = new THREE.PerspectiveCamera(42, 1, 0.1, 100);
    camera.position.set(0, 0.35, 3.05);
    camera.lookAt(0, 0, 0);

    build();
    resize();
    window.addEventListener('resize', resize, { passive: true });

    reduced = window.matchMedia &&
      window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    if (reduced) { render(0); return true; }

    document.addEventListener('visibilitychange', () => {
      visible = !document.hidden;
      visible ? start() : stop();
    });
    if ('IntersectionObserver' in window) {
      new IntersectionObserver(([e]) => {
        visible = e.isIntersecting && !document.hidden;
        visible ? start() : stop();
      }, { threshold: 0 }).observe(host);
    }

    bindScroll();
    start();
    started = true;
    return true;
  }

  return { init, start, stop, get isRunning() { return frame !== null; } };
})();

window.Globe = Globe;
