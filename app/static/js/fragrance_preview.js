// Port of apps.scent-library.fragrance-preview.jsx's client behavior -- React state/hooks
// replaced with plain DOM state and event listeners, but the same interactions, the same
// adjustRatios redistribution math, and the same three.js bottle. Data that used to arrive via
// useLoaderData() arrives the same way here: server-rendered once, then read from
// window.__PREVIEW_DATA__ (see fragrance_preview.html).
(function () {
  "use strict";

  const DATA = window.__PREVIEW_DATA__;
  const BOTTLE_ML = 34;
  const MIN_PCT = 5;
  const OVERLAY_MESSAGES = { recreate: "Returning to your conversation…", save_build: "Creating your fragrance…", add_to_cart: "Adding your fragrance to cart…" };

  const state = { name: DATA.name, ratios: { ...DATA.ratios } };

  // ---- estimateTotalPrice, port of app/utils/fragrancePricing.js ----
  function estimateTotalPrice(pricePer5mlByPosition, ratios) {
    return ["top", "middle", "base"].reduce((sum, position) => {
      const ml = ((ratios[position] || 0) / 100) * BOTTLE_ML;
      return sum + (pricePer5mlByPosition[position] / 5) * ml;
    }, 0);
  }

  // ---- adjustRatios, byte-identical to the JS original's redistribution logic ----
  function adjustRatios(current, changedKey, newValue) {
    const keys = Object.keys(current);
    const others = keys.filter((k) => k !== changedKey);
    if (others.length === 0) return { ...current, [changedKey]: 100 };

    const maxPct = 100 - MIN_PCT * others.length;
    const newPct = Math.max(MIN_PCT, Math.min(maxPct, newValue));
    const delta = newPct - current[changedKey];

    const updated = { ...current, [changedKey]: newPct };
    const othersTotal = others.reduce((s, k) => s + current[k], 0);
    if (othersTotal > 0) {
      others.forEach((k) => {
        const share = current[k] / othersTotal;
        updated[k] = Math.max(MIN_PCT, current[k] - delta * share);
      });
    }
    const total = keys.reduce((s, k) => s + updated[k], 0);
    updated[changedKey] += 100 - total;

    const rounded = Object.fromEntries(Object.entries(updated).map(([k, v]) => [k, Math.round(v)]));
    const diff = 100 - Object.values(rounded).reduce((a, b) => a + b, 0);
    if (diff !== 0) {
      const largest = Object.entries(rounded).sort((a, b) => b[1] - a[1])[0][0];
      rounded[largest] += diff;
    }
    return rounded;
  }

  // ---- DOM refs ----
  const titleInput = document.getElementById("preview-title");
  const priceEl = document.getElementById("preview-price");
  const errorEl = document.getElementById("preview-error");
  const loadingOverlay = document.getElementById("preview-loading");
  const loadingText = document.getElementById("preview-loading-text");
  const rows = { top: document.querySelector('.cs-note-row[data-position="top"]'), middle: document.querySelector('.cs-note-row[data-position="middle"]'), base: document.querySelector('.cs-note-row[data-position="base"]') };
  const buttons = { recreate: document.getElementById("btn-recreate"), save_build: document.getElementById("btn-save-build"), add_to_cart: document.getElementById("btn-add-to-cart") };

  function autoGrowTitle() {
    titleInput.style.height = "auto";
    titleInput.style.height = titleInput.scrollHeight + "px";
  }
  titleInput.addEventListener("input", () => { state.name = titleInput.value; autoGrowTitle(); });
  autoGrowTitle();

  function renderRatios() {
    for (const position of ["top", "middle", "base"]) {
      const pct = Math.round(state.ratios[position]);
      const row = rows[position];
      row.querySelector(".cs-note-pct").textContent = pct + "%";
      const slider = row.querySelector(".cs-slider");
      slider.value = pct;
      const fillColor = { top: "#2655d8", middle: "#D9AE68", base: "#8C4A3C" }[position];
      slider.style.background = `linear-gradient(to right, ${fillColor} ${pct}%, var(--cs-taupe-light) ${pct}%)`;
    }
    priceEl.textContent = "$" + estimateTotalPrice(DATA.pricePer5mlByPosition, state.ratios).toFixed(2);
    if (window.__updateBottleLayers) {
      window.__updateBottleLayers(["top", "middle", "base"].map((position) => ({ position, pct: state.ratios[position] })));
    }
  }

  for (const position of ["top", "middle", "base"]) {
    rows[position].querySelector(".cs-slider").addEventListener("input", (e) => {
      state.ratios = adjustRatios(state.ratios, position, Number(e.target.value));
      renderRatios();
    });
  }
  renderRatios();

  // ---- submit / intent handling ----
  let pendingIntent = null;

  function setBusy(intent) {
    pendingIntent = intent;
    Object.values(buttons).forEach((b) => (b.disabled = true));
    loadingText.textContent = OVERLAY_MESSAGES[intent];
    loadingOverlay.style.display = "flex";
    errorEl.style.display = "none";
  }

  function clearBusy() {
    pendingIntent = null;
    Object.values(buttons).forEach((b) => (b.disabled = false));
    loadingOverlay.style.display = "none";
  }

  function showError(message) {
    clearBusy();
    errorEl.textContent = message;
    errorEl.style.display = "block";
  }

  async function submit(intent) {
    setBusy(intent);
    let response, json;
    try {
      response = await fetch(window.location.pathname + window.location.search, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        // buildToken: the server-minted capability for this one build (see docs/SHOPIFY_BUILD_SECURITY_CONTRACT.md).
        body: JSON.stringify({ intent, recommendationId: DATA.recommendationId, buildToken: DATA.buildToken, name: state.name, ratios: state.ratios }),
      });
      json = await response.json();
    } catch (err) {
      showError("Failed to save the build.");
      return;
    }

    if (json.error) {
      showError(json.error);
      return;
    }
    if (json.status === "recreate" && json.redirectUrl) {
      window.location.href = json.redirectUrl;
    } else if (json.status === "saved" && json.productUrl) {
      // The capability travels to the product page in the URL fragment (never sent to any
      // server, never in referrers) so the theme's slider can call /api/save-build with it.
      window.location.href = json.productUrl + "#scentBuild=" + encodeURIComponent(DATA.recommendationId) + "." + encodeURIComponent(DATA.buildToken);
    } else if (json.status === "added" && json.cartUrl) {
      window.location.href = json.cartUrl;
    } else {
      showError("Unexpected response from the server.");
    }
  }

  buttons.recreate.addEventListener("click", () => submit("recreate"));
  buttons.save_build.addEventListener("click", () => submit("save_build"));
  buttons.add_to_cart.addEventListener("click", () => submit("add_to_cart"));

  // ---- three.js bottle, ported near-verbatim from BottleVisualization's effect body ----
  (async function initBottle() {
    const mount = document.getElementById("cs-bottle-3d");
    if (!mount) return;
    const THREE = await import("https://unpkg.com/three@0.160.0/build/three.module.js");
    const { RoomEnvironment } = await import("https://unpkg.com/three@0.160.0/examples/jsm/environments/RoomEnvironment.js");

    const scene = new THREE.Scene();
    scene.background = new THREE.Color(0xe5ded3);

    const camera = new THREE.PerspectiveCamera(35, 1, 0.1, 100);
    camera.position.set(0, 1.8, 7.5);
    camera.lookAt(0, 1.5, 0);

    const renderer = new THREE.WebGLRenderer({ antialias: true });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    renderer.toneMapping = THREE.ACESFilmicToneMapping;
    renderer.toneMappingExposure = 1.1;
    mount.appendChild(renderer.domElement);

    try {
      const pmrem = new THREE.PMREMGenerator(renderer);
      scene.environment = pmrem.fromScene(new RoomEnvironment(), 0.04).texture;
    } catch (e) {
      console.warn("Environment setup failed, continuing without reflections:", e);
    }

    const keyLight = new THREE.DirectionalLight(0xffffff, 2.2);
    keyLight.position.set(3, 6, 4);
    scene.add(keyLight);
    const fillLight = new THREE.DirectionalLight(0xffffff, 0.6);
    fillLight.position.set(-4, 2, -3);
    scene.add(fillLight);
    scene.add(new THREE.AmbientLight(0xffffff, 0.4));

    const bottleGroup = new THREE.Group();
    scene.add(bottleGroup);

    function roundedRectShape(w, h, r) {
      const shape = new THREE.Shape();
      const x = -w / 2, y = -h / 2;
      shape.moveTo(x, y + r);
      shape.lineTo(x, y + h - r);
      shape.quadraticCurveTo(x, y + h, x + r, y + h);
      shape.lineTo(x + w - r, y + h);
      shape.quadraticCurveTo(x + w, y + h, x + w, y + h - r);
      shape.lineTo(x + w, y + r);
      shape.quadraticCurveTo(x + w, y, x + w - r, y);
      shape.lineTo(x + r, y);
      shape.quadraticCurveTo(x, y, x, y + r);
      return shape;
    }

    const BODY_WIDTH = 1.7, BODY_HEIGHT = 2.7, BODY_DEPTH = 0.7, BODY_RADIUS = 0.35;
    const glassShape = roundedRectShape(BODY_WIDTH, BODY_HEIGHT, BODY_RADIUS);
    const glassGeometry = new THREE.ExtrudeGeometry(glassShape, { depth: BODY_DEPTH, bevelEnabled: true, bevelThickness: 0.06, bevelSize: 0.06, bevelSegments: 6, curveSegments: 16 });
    glassGeometry.translate(0, 0, -BODY_DEPTH / 2);

    const glassMaterial = new THREE.MeshPhysicalMaterial({ color: 0xffffff, transmission: 0.95, thickness: 0.5, roughness: 0.04, ior: 1.5, envMapIntensity: 1.3 });
    const glassMesh = new THREE.Mesh(glassGeometry, glassMaterial);
    glassMesh.position.y = BODY_HEIGHT / 2 + 0.1;
    bottleGroup.add(glassMesh);

    const BODY_TOP_Y = BODY_HEIGHT + 0.1;

    const neckGeometry = new THREE.CylinderGeometry(0.32, 0.36, 0.3, 24);
    const neckMesh = new THREE.Mesh(neckGeometry, glassMaterial);
    neckMesh.position.y = BODY_TOP_Y + 0.15;
    bottleGroup.add(neckMesh);

    const collarGeometry = new THREE.CylinderGeometry(0.4, 0.4, 0.1, 24);
    const collarMaterial = new THREE.MeshStandardMaterial({ color: 0xc9a24a, roughness: 0.35, metalness: 0.3 });
    const collarMesh = new THREE.Mesh(collarGeometry, collarMaterial);
    collarMesh.position.y = BODY_TOP_Y + 0.35;
    bottleGroup.add(collarMesh);

    const capGeometry = new THREE.CylinderGeometry(0.42, 0.4, 0.55, 24);
    const capMaterial = new THREE.MeshStandardMaterial({ color: 0xc9a06a, roughness: 0.6, metalness: 0.05 });
    const capMesh = new THREE.Mesh(capGeometry, capMaterial);
    capMesh.position.y = BODY_TOP_Y + 0.35 + 0.325;
    bottleGroup.add(capMesh);

    const LIQUID_BOTTOM_Y = 0.08;
    const LIQUID_TOP_Y = BODY_HEIGHT - 0.15;
    const LIQUID_HEIGHT = LIQUID_TOP_Y - LIQUID_BOTTOM_Y;

    function customRoundedRectShape(w, h, rTL, rTR, rBR, rBL) {
      const shape = new THREE.Shape();
      const x = -w / 2, y = -h / 2;
      shape.moveTo(x, y + rBL);
      shape.lineTo(x, y + h - rTL);
      shape.quadraticCurveTo(x, y + h, x + rTL, y + h);
      shape.lineTo(x + w - rTR, y + h);
      shape.quadraticCurveTo(x + w, y + h, x + w, y + h - rTR);
      shape.lineTo(x + w, y + rBR);
      shape.quadraticCurveTo(x + w, y, x + w - rBR, y);
      shape.lineTo(x + rBL, y);
      shape.quadraticCurveTo(x, y, x, y + rBL);
      return shape;
    }

    function makeHorizontalUVGenerator(width) {
      function uvFromX(x) { return THREE.MathUtils.clamp((x + width / 2) / width, 0, 1); }
      return {
        generateTopUV: function (geometry, vertices, a, b, c) {
          return [new THREE.Vector2(uvFromX(vertices[a * 3]), 0.5), new THREE.Vector2(uvFromX(vertices[b * 3]), 0.5), new THREE.Vector2(uvFromX(vertices[c * 3]), 0.5)];
        },
        generateSideWallUV: function (geometry, vertices, a, b, c, d) {
          return [new THREE.Vector2(uvFromX(vertices[a * 3]), 0), new THREE.Vector2(uvFromX(vertices[b * 3]), 0), new THREE.Vector2(uvFromX(vertices[c * 3]), 1), new THREE.Vector2(uvFromX(vertices[d * 3]), 1)];
        },
      };
    }

    function makeGradientTexture(hexStart, hexEnd) {
      const canvas = document.createElement("canvas");
      canvas.width = 64; canvas.height = 4;
      const ctx = canvas.getContext("2d");
      const grad = ctx.createLinearGradient(0, 0, canvas.width, 0);
      grad.addColorStop(0, hexStart);
      grad.addColorStop(1, hexEnd);
      ctx.fillStyle = grad;
      ctx.fillRect(0, 0, canvas.width, canvas.height);
      const tex = new THREE.CanvasTexture(canvas);
      tex.colorSpace = THREE.SRGBColorSpace;
      return tex;
    }

    const LAYER_GRADIENTS = { top: ["#0a1a4a", "#2655d8"], middle: ["#4a2800", "#f09000"], base: ["#6B372C", "#8C4A3C"] };

    const LIQUID_WIDTH = BODY_WIDTH - 0.08;
    const LIQUID_DEPTH = BODY_DEPTH - 0.08;
    const LIQUID_RADIUS = BODY_RADIUS - 0.03;
    const uvGen = makeHorizontalUVGenerator(LIQUID_WIDTH);

    const topShape = customRoundedRectShape(LIQUID_WIDTH, 1, LIQUID_RADIUS, LIQUID_RADIUS, 0, 0);
    const middleShape = customRoundedRectShape(LIQUID_WIDTH, 1, 0, 0, 0, 0);
    const baseShape = customRoundedRectShape(LIQUID_WIDTH, 1, 0, 0, LIQUID_RADIUS, LIQUID_RADIUS);

    function makeLiquidGeometry(shape) {
      const geo = new THREE.ExtrudeGeometry(shape, { depth: LIQUID_DEPTH, bevelEnabled: true, bevelThickness: 0.025, bevelSize: 0.025, bevelSegments: 6, curveSegments: 16, UVGenerator: uvGen });
      geo.translate(0, 0, -LIQUID_DEPTH / 2);
      return geo;
    }

    function makeLiquidMesh(shape, gradientStops) {
      const mat = new THREE.MeshPhysicalMaterial({ map: makeGradientTexture(gradientStops[0], gradientStops[1]), roughness: 0.12, clearcoat: 0.6, clearcoatRoughness: 0.08, envMapIntensity: 1.1 });
      const mesh = new THREE.Mesh(makeLiquidGeometry(shape), mat);
      mesh.position.y = BODY_HEIGHT / 2 + 0.1;
      bottleGroup.add(mesh);
      return mesh;
    }

    const liquidMeshes = { base: makeLiquidMesh(baseShape, LAYER_GRADIENTS.base), middle: makeLiquidMesh(middleShape, LAYER_GRADIENTS.middle), top: makeLiquidMesh(topShape, LAYER_GRADIENTS.top) };

    function updateLiquidLayers(stateArr) {
      const pctByPosition = { top: 0, middle: 0, base: 0 };
      stateArr.forEach((item) => { pctByPosition[item.position] = item.pct; });

      const order = ["base", "middle", "top"];
      let cursorY = LIQUID_BOTTOM_Y;
      const bodyCenterY = BODY_HEIGHT / 2 + 0.1;

      order.forEach((position) => {
        const pct = pctByPosition[position] || 0;
        const h = Math.max(0.001, (pct / 100) * LIQUID_HEIGHT);
        const mesh = liquidMeshes[position];
        mesh.scale.y = h;
        mesh.position.y = bodyCenterY - BODY_HEIGHT / 2 + cursorY + h / 2;
        cursorY += h;
      });
    }
    window.__updateBottleLayers = updateLiquidLayers;

    function resize() {
      const w = mount.clientWidth || 300;
      const h = mount.clientHeight || 500;
      camera.aspect = w / h;
      camera.updateProjectionMatrix();
      renderer.setSize(w, h);
    }
    window.addEventListener("resize", resize);
    resize();

    let isDragging = false, lastX = 0, targetRotationY = 0.4, idleTimeout = null, autoRotate = true;
    function onPointerDown(e) { isDragging = true; autoRotate = false; lastX = e.touches ? e.touches[0].clientX : e.clientX; mount.style.cursor = "grabbing"; clearTimeout(idleTimeout); }
    function onPointerMove(e) { if (!isDragging) return; const x = e.touches ? e.touches[0].clientX : e.clientX; targetRotationY += (x - lastX) * 0.01; lastX = x; }
    function onPointerUp() { isDragging = false; mount.style.cursor = "grab"; idleTimeout = setTimeout(() => { autoRotate = true; }, 1800); }
    mount.addEventListener("pointerdown", onPointerDown);
    window.addEventListener("pointermove", onPointerMove);
    window.addEventListener("pointerup", onPointerUp);
    mount.addEventListener("touchstart", onPointerDown, { passive: true });
    window.addEventListener("touchmove", onPointerMove, { passive: true });
    window.addEventListener("touchend", onPointerUp);

    function animate() {
      requestAnimationFrame(animate);
      if (autoRotate) targetRotationY += 0.0025;
      bottleGroup.rotation.y += (targetRotationY - bottleGroup.rotation.y) * 0.08;
      renderer.render(scene, camera);
    }
    animate();

    updateLiquidLayers(["top", "middle", "base"].map((position) => ({ position, pct: state.ratios[position] })));
  })().catch((err) => console.error("Bottle visualization failed to load:", err));
})();
