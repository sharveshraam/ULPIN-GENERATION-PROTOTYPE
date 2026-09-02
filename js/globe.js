/**
 * Rotating background globe for the landing page.
 *
 * A "digital twin" style wireframe Earth: a Fibonacci-distributed point cloud
 * on the sphere, latitude/longitude wireframe, a soft atmosphere shell, and
 * pulsing markers at real Indian cities (this is an India-focused product, so
 * the markers are thematic rather than decorative noise).
 *
 * Design constraints:
 *   - Purely decorative and behind all content: pointer-events none, aria-hidden.
 *   - Never breaks the page. If WebGL is missing or three.js failed to load,
 *     it silently does nothing and the CSS aurora remains as the backdrop.
 *   - Honours prefers-reduced-motion by rendering a single static frame.
 *   - Pauses when the tab is hidden or the hero is scrolled out of view, so it
 *     costs nothing while the user reads the rest of the page.
 *
 * Uses three.js r0.128.0 UMD globals (THREE.*), matching the pin in map.html.
 * Do not "upgrade" without switching to an importmap: r148+ dropped
 * examples/js/ and r150+ dropped build/three.min.js.
 */
const Globe = (() => {
  let renderer, scene, camera, root, frame = null, host = null;
  let visible = true, reduced = false;

  // Real coordinates, so the markers mean something.
  const CITIES = [
    { name: 'Delhi',        lat: 28.6139, lon: 77.2090 },
    { name: 'Mumbai',       lat: 19.0760, lon: 72.8777 },
    { name: 'Kolkata',      lat: 22.5726, lon: 88.3639 },
    { name: 'Chennai',      lat: 13.0827, lon: 80.2707 },
    { name: 'Bengaluru',    lat: 12.9716, lon: 77.5946 },
    { name: 'Kochi',        lat:  9.9312, lon: 76.2673 },
    { name: 'Hyderabad',    lat: 17.3850, lon: 78.4867 },
    { name: 'Ahmedabad',    lat: 23.0225, lon: 72.5714 },
  ];

  const RADIUS = 1;

  /** Lat/lon (degrees) -> point on a sphere of the given radius. */
  function toVector(lat, lon, radius = RADIUS) {
    const phi = (90 - lat) * Math.PI / 180;
    const theta = (lon + 180) * Math.PI / 180;
    return new THREE.Vector3(
      -radius * Math.sin(phi) * Math.cos(theta),
       radius * Math.cos(phi),
       radius * Math.sin(phi) * Math.sin(theta)
    );
  }

  /** Evenly spread N points over the sphere (Fibonacci lattice). */
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

  function build() {
    root = new THREE.Group();

    // Wireframe shell — the recognisable "globe" read.
    root.add(new THREE.LineSegments(
      new THREE.WireframeGeometry(new THREE.SphereGeometry(RADIUS, 36, 24)),
      new THREE.LineBasicMaterial({ color: 0x14b8a6, transparent: true, opacity: 0.13, depthWrite: false })
    ));

    // Solid core, slightly inset, so back-facing dots are occluded and the
    // sphere reads as a volume rather than a flat scatter.
    root.add(new THREE.Mesh(
      new THREE.SphereGeometry(RADIUS * 0.985, 48, 32),
      new THREE.MeshBasicMaterial({ color: 0x061620, transparent: true, opacity: 0.92 })
    ));

    root.add(pointCloud(1400));

    // Atmosphere: a back-side shell fakes a rim glow without post-processing.
    root.add(new THREE.Mesh(
      new THREE.SphereGeometry(RADIUS * 1.055, 48, 32),
      new THREE.MeshBasicMaterial({
        color: 0x22d3ee, transparent: true, opacity: 0.055,
        side: THREE.BackSide, depthWrite: false,
      })
    ));

    // City markers.
    const markers = new THREE.Group();
    CITIES.forEach(c => {
      const p = toVector(c.lat, c.lon, RADIUS * 1.005);
      const dot = new THREE.Mesh(
        new THREE.SphereGeometry(0.014, 10, 10),
        new THREE.MeshBasicMaterial({ color: 0x5eead4 })
      );
      dot.position.copy(p);
      markers.add(dot);

      const halo = new THREE.Mesh(
        new THREE.SphereGeometry(0.03, 12, 12),
        new THREE.MeshBasicMaterial({ color: 0x2dd4bf, transparent: true, opacity: 0.3, depthWrite: false })
      );
      halo.position.copy(p);
      halo.userData.phase = Math.random() * Math.PI * 2;
      markers.add(halo);
    });
    root.userData.markers = markers;
    root.add(markers);

    // Tilt like an axial tilt; reads as a planet rather than a ball.
    root.rotation.z = 0.36;
    scene.add(root);
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
    root.rotation.y += 0.0016;
    render(t);
    frame = requestAnimationFrame(loop);
  }

  function start() {
    if (frame === null && visible && !reduced) frame = requestAnimationFrame(loop);
  }
  function stop() {
    if (frame !== null) { cancelAnimationFrame(frame); frame = null; }
  }

  function init(hostId = 'globe') {
    host = document.getElementById(hostId);
    if (!host) return false;

    // three.js absent (CDN blocked) — leave the CSS backdrop in place.
    if (typeof THREE === 'undefined') return false;

    try {
      renderer = new THREE.WebGLRenderer({ alpha: true, antialias: true });
    } catch (_) {
      return false;                        // no WebGL: fail silently
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
    if (reduced) { render(0); return true; }   // one static frame, no animation

    // Don't burn cycles on a hidden tab or an off-screen hero.
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

    start();
    return true;
  }

  return { init, start, stop };
})();

window.Globe = Globe;
