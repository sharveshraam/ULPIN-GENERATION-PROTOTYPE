/**
 * Three.js building viewer.
 *
 * Renders a real footprint extruded floor by floor. Each floor is a separate
 * mesh so it can be hovered, clicked and colour-banded. Footprint coordinates
 * are projected from lon/lat to local metres before extrusion, otherwise a
 * building would be ~100000x wider than it is tall.
 */
const Viewer3D = (() => {
  let renderer, scene, camera, controls, raycaster, pointer;
  let floorMeshes = [];
  let model = null;
  let hovered = null;
  let animationId = null;
  let container = null;
  let onFloorPick = null;

  const M_PER_DEG_LAT = 110574;

  /* Colour band per 10 floors, low -> high. */
  function bandColor(floorNumber, totalFloors) {
    const t = totalFloors > 1 ? (floorNumber - 1) / (totalFloors - 1) : 0;
    const stops = [
      [0.00, [0x22, 0xd3, 0xee]],
      [0.35, [0x38, 0xbd, 0xf8]],
      [0.60, [0x81, 0x8c, 0xf8]],
      [0.80, [0xf9, 0x73, 0x16]],
      [1.00, [0xf4, 0x3f, 0x5e]],
    ];
    let a = stops[0], b = stops[stops.length - 1];
    for (let i = 0; i < stops.length - 1; i++) {
      if (t >= stops[i][0] && t <= stops[i + 1][0]) { a = stops[i]; b = stops[i + 1]; break; }
    }
    const span = (b[0] - a[0]) || 1;
    const k = (t - a[0]) / span;
    const rgb = a[1].map((c, i) => Math.round(c + (b[1][i] - c) * k));
    return (rgb[0] << 16) | (rgb[1] << 8) | rgb[2];
  }

  /** Project lon/lat ring to local metres centred on the footprint. */
  function projectRing(ring) {
    const lats = ring.map(p => p[1]);
    const lons = ring.map(p => p[0]);
    const lat0 = (Math.min(...lats) + Math.max(...lats)) / 2;
    const lon0 = (Math.min(...lons) + Math.max(...lons)) / 2;
    const mLon = 111320 * Math.cos(lat0 * Math.PI / 180);
    return ring.map(([lon, lat]) => [(lon - lon0) * mLon, (lat - lat0) * M_PER_DEG_LAT]);
  }

  function init(el) {
    if (renderer) return;              // already initialised
    container = el;
    const w = el.clientWidth || 800;
    const h = el.clientHeight || 500;

    scene = new THREE.Scene();
    scene.background = new THREE.Color(0x080c17);
    scene.fog = new THREE.Fog(0x080c17, 400, 2600);

    camera = new THREE.PerspectiveCamera(50, w / h, 0.5, 12000);
    camera.position.set(160, 130, 200);

    renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    renderer.setSize(w, h);
    el.appendChild(renderer.domElement);

    controls = new THREE.OrbitControls(camera, renderer.domElement);
    controls.enableDamping = true;
    controls.dampingFactor = 0.07;
    controls.maxPolarAngle = Math.PI * 0.495;   // stay above ground

    scene.add(new THREE.HemisphereLight(0xbfd8ff, 0x0a0f1c, 1.05));
    const key = new THREE.DirectionalLight(0xffffff, 1.15);
    key.position.set(180, 320, 160);
    scene.add(key);
    const rim = new THREE.DirectionalLight(0x6366f1, 0.55);
    rim.position.set(-200, 120, -180);
    scene.add(rim);

    raycaster = new THREE.Raycaster();
    pointer = new THREE.Vector2();

    renderer.domElement.addEventListener('pointermove', onPointerMove);
    renderer.domElement.addEventListener('click', onClick);
    window.addEventListener('resize', resize);

    animate();
  }

  function clearScene() {
    floorMeshes.forEach((m) => {
      scene.remove(m);
      m.geometry.dispose();
      m.material.dispose();
    });
    floorMeshes = [];
    // Remove helpers (grid/ground) too.
    [...scene.children].forEach((c) => {
      if (c.userData.isHelper) {
        scene.remove(c);
        c.geometry?.dispose?.();
        c.material?.dispose?.();
      }
    });
  }

  /**
   * @param {object} data  Response from /api/v1/generate-3d-model
   * @param {number[][]} ring  Footprint as [[lon,lat],...]
   */
  function render(data, ring) {
    model = data;
    clearScene();

    const pts = projectRing(ring);
    const shape = new THREE.Shape();
    pts.forEach(([x, y], i) => (i === 0 ? shape.moveTo(x, y) : shape.lineTo(x, y)));
    shape.closePath();

    const floors = data.floors || [];
    const total = floors.length || 1;

    // Ground plane + grid for scale.
    const extent = Math.max(
      ...pts.map(([x, y]) => Math.max(Math.abs(x), Math.abs(y)))
    ) * 4 + 60;
    const grid = new THREE.GridHelper(extent * 2, 26, 0x1e293b, 0x131c2e);
    grid.position.y = -0.4;
    grid.userData.isHelper = true;
    scene.add(grid);

    let maxTop = 0;
    floors.forEach((f) => {
      const thickness = Math.max(0.6, f.floor_height_m * 0.82); // gap reads as a slab line
      const geom = new THREE.ExtrudeGeometry(shape, { depth: thickness, bevelEnabled: false });
      geom.rotateX(-Math.PI / 2);   // XY shape -> XZ ground plane

      const isMech = f.floor_type === 'mechanical';
      const mat = new THREE.MeshStandardMaterial({
        color: isMech ? 0x64748b : bandColor(f.floor_number, total),
        metalness: 0.25,
        roughness: 0.55,
        transparent: true,
        opacity: isMech ? 0.95 : 0.88,
      });

      const mesh = new THREE.Mesh(geom, mat);
      mesh.position.y = f.base_elevation_m;
      mesh.userData.floor = f;
      mesh.userData.baseColor = mat.color.getHex();
      scene.add(mesh);
      floorMeshes.push(mesh);
      maxTop = Math.max(maxTop, f.base_elevation_m + thickness);
    });

    // Frame the whole tower.
    const dist = Math.max(extent * 1.4, maxTop * 1.25, 60);
    camera.position.set(dist * 0.75, Math.max(maxTop * 0.65, 40), dist);
    controls.target.set(0, maxTop * 0.42, 0);
    controls.update();

    return { floors: total, height: maxTop };
  }

  function onPointerMove(e) {
    const r = renderer.domElement.getBoundingClientRect();
    pointer.x = ((e.clientX - r.left) / r.width) * 2 - 1;
    pointer.y = -((e.clientY - r.top) / r.height) * 2 + 1;

    raycaster.setFromCamera(pointer, camera);
    const hit = raycaster.intersectObjects(floorMeshes, false)[0];

    if (hovered && (!hit || hit.object !== hovered)) {
      hovered.material.emissive?.setHex(0x000000);
      hovered.material.opacity = hovered.userData.floor.floor_type === 'mechanical' ? 0.95 : 0.88;
      hovered = null;
    }
    const tip = document.getElementById('viewer3dTip');
    if (hit) {
      hovered = hit.object;
      hovered.material.emissive?.setHex(0x2563eb);
      hovered.material.opacity = 1;
      const f = hovered.userData.floor;
      if (tip) {
        tip.innerHTML =
          `<b class="text-sky-300">Floor ${f.floor_number}</b> · ${f.floor_type}` +
          `<br><span class="text-slate-400">${f.units_on_floor} units · ${f.floor_height_m} m ·` +
          ` elev ${f.base_elevation_m} m</span>` +
          `<br><span class="font-mono text-[10px] text-slate-500">${f.floor_ulpin}</span>`;
        tip.classList.remove('hidden');
        tip.style.left = `${e.clientX - r.left + 14}px`;
        tip.style.top = `${e.clientY - r.top + 14}px`;
      }
      renderer.domElement.style.cursor = 'pointer';
    } else {
      tip?.classList.add('hidden');
      renderer.domElement.style.cursor = 'grab';
    }
  }

  function onClick() {
    if (hovered && onFloorPick) onFloorPick(hovered.userData.floor);
  }

  function resize() {
    if (!container || !renderer) return;
    const w = container.clientWidth, h = container.clientHeight;
    if (!w || !h) return;
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
    renderer.setSize(w, h);
  }

  function animate() {
    animationId = requestAnimationFrame(animate);
    controls?.update();
    renderer?.render(scene, camera);
  }

  function highlightFloor(floorNumber) {
    floorMeshes.forEach((m) => {
      const on = m.userData.floor.floor_number === floorNumber;
      m.material.emissive?.setHex(on ? 0x22c55e : 0x000000);
      m.material.opacity = on ? 1 : 0.55;
    });
  }

  function resetHighlight() {
    floorMeshes.forEach((m) => {
      m.material.emissive?.setHex(0x000000);
      m.material.opacity = m.userData.floor.floor_type === 'mechanical' ? 0.95 : 0.88;
    });
  }

  return {
    init, render, resize, highlightFloor, resetHighlight,
    set onFloorPick(fn) { onFloorPick = fn; },
    get model() { return model; },
    dispose() {
      cancelAnimationFrame(animationId);
      clearScene();
      renderer?.dispose();
      renderer = null;
    },
  };
})();

window.Viewer3D = Viewer3D;
