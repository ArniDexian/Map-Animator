// Verify the actual style-preview geometry without a browser or screen access.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.resolve(__dirname, '..');
const html = fs.readFileSync(path.join(root, 'ui_static/index.html'), 'utf8');
const source = html.slice(html.indexOf('const endpointSampleImages ='), html.indexOf('window.addEventListener("resize", drawPreview)'));
assert(source.includes('function drawPreview()'));
for (const width of [320, 510, 960]) {
  const curves = [], circles = [], texts = [], draws = [], pendingImages = [], transforms = [];
  let lastMove;
  const context = {
    setTransform() {}, fillRect() {}, beginPath() {}, lineTo() {}, stroke() {}, fill() {}, closePath() {},
    save() {}, restore() {}, translate(...args) { transforms.push(["translate", ...args]); }, rotate(...args) { transforms.push(["rotate", ...args]); },
    createLinearGradient() { return {addColorStop() {}}; },
    moveTo(x, y) { lastMove = [x, y]; },
    bezierCurveTo(...coords) { curves.push([lastMove, ...[0, 2, 4].map(i => coords.slice(i, i + 2))]); },
    arc(x, y) { circles.push([x, y]); },
    measureText() { return {actualBoundingBoxLeft:0, actualBoundingBoxRight:32, actualBoundingBoxDescent:4}; },
    fillText(...args) { texts.push(args); },
    drawImage(...args) { draws.push(args); },
  };
  const canvas = {clientWidth: width, getContext() { return context; }};
  const cfg = JSON.parse(fs.readFileSync(path.join(root, 'config.json'), 'utf8'));
  cfg.mode = 'follow'; cfg.follow_settings.start_icon = 'green_flag'; // Explicit legacy geometry fixture.
  const sandbox = {cfg, analysis: null, images: {}, TOKEN: "test-token", window: {devicePixelRatio: 1},
    previewBg: null, previewBgUrl: null,
    emojiURL: (emoji, size) => '/api/emoji?value=' + encodeURIComponent(emoji) + '&size=' + size,
    Image: class { constructor() { this.complete = false; this.naturalWidth = 0; pendingImages.push(this); } set src(url) { this.url = url; } },
    $: selector => selector === '#preview' ? canvas : {hidden: false}};
  vm.runInNewContext(source + '\ndrawPreview();', sandbox);
  assert.equal(curves.length, 2, 'follow preview draws one curve for each route leg');
  assert.equal(circles.length, 2); // Outline and fill must share the same on-path center.
  const curve = curves[0];
  const expected = [0, 1].map(axis => (curve[0][axis] + 3 * curve[1][axis] + 3 * curve[2][axis] + curve[3][axis]) / 8);
  for (const circle of circles) {
    circle.forEach((value, axis) => assert(Math.abs(value - expected[axis]) < 1e-9,
      `Waypoint is off the preview curve at width ${width}`));
  }
  circles.length = 0; cfg.follow_settings.hide_waypoints = true;
  vm.runInNewContext('drawPreview();', sandbox);
  assert.equal(circles.length, 0, 'Hidden waypoints must disappear from the style preview');
  curves.length = 0; cfg.follow_settings.start_icon = 'pin';
  vm.runInNewContext("drawEndpointSample($('#preview').getContext('2d'), cfg.follow_settings, 'start', 120, 70, 1);", sandbox);
  assert.equal(curves.length, 4, 'Pin must have a continuous curved silhouette');
  assert.equal(circles.length, 1, 'Pin must have a visible circular center');
  cfg.follow_settings.start_icon = 'emoji'; cfg.follow_settings.start_emoji = '🌳';
  vm.runInNewContext("drawEndpointSample($('#preview').getContext('2d'), cfg.follow_settings, 'start', 120, 70, 1);", sandbox);
  assert.equal(draws.length, 0, 'Unloaded emoji must not be drawn as a tinted text fallback');
  assert.equal(texts.length, 0, 'Emoji must use the video PNG, not browser font state');
  const image = pendingImages.find(candidate => candidate.url && candidate.url.includes(encodeURIComponent("🌳")));
  assert(image, 'The selected emoji should request its own preview image');
  image.complete = true; image.naturalWidth = 128; image.naturalHeight = 128;
  image.onload();
  assert.equal(draws[0][0], image, 'First completed emoji request must immediately repaint the sample');
  assert(image.url.startsWith('/api/emoji?'));
  draws.length = 0; cfg.follow_settings.start_offset_x_px = 5; cfg.follow_settings.start_offset_y_px = -7;
  vm.runInNewContext("drawEndpointSample($('#preview').getContext('2d'), cfg.follow_settings, 'start', 120, 70, 1);", sandbox);
  assert.equal(draws[0][1], 125 - cfg.follow_settings.start_size_px / 2);
  assert.equal(draws[0][2], 63 - cfg.follow_settings.start_size_px);

  draws.length = 0; curves.length = 0; cfg.follow_settings.start_size_px = 512; cfg.follow_settings.end_size_px = 512;
  cfg.follow_settings.start_icon = "custom"; cfg.follow_settings.end_icon = "custom";
  cfg.follow_settings.start_image = "start-image"; cfg.follow_settings.end_image = "end-image";
  sandbox.images.clean = {url:"clean-map"};
  vm.runInNewContext('drawPreview();', sandbox);
  const map = pendingImages.find(candidate => candidate.url === "clean-map");
  assert(map, 'The clean map should load for the preview background');
  map.complete = true; map.naturalWidth = 400; map.naturalHeight = 400; map.onload();
  assert(draws.some(args => args.length === 9 && args[7] === width && args[8] === 80),
    `The map background should cover the entire preview at width ${width}`);
  assert(curves[0][0][0] >= width * .06,
    `Large endpoint icons should not push the route beyond its preview margins at width ${width}`);
  if (width <= 510) assert(curves[0][0][0] > width * .06,
    `Large endpoint icons should shorten the preview route at width ${width}`);
  transforms.length = 0;
  sandbox.cfg.follow_settings.object_icon = "custom";
  sandbox.cfg.follow_settings.object_image = "object-key";
  sandbox.cfg.follow_settings.object_size_px = 64;
  sandbox.cfg.follow_settings.object_orientation = "down";
  sandbox.cfg.follow_settings.object_anchor = "left";
  vm.runInNewContext('followObjectImages.set("object-key", {complete:true, naturalWidth:20, naturalHeight:10}); drawFollowObjectSample($("#preview").getContext("2d"), cfg.follow_settings, 200, 40, 1, 0);', sandbox);
  assert.deepEqual(transforms[0], ["translate", 200, 40]);
  assert(Math.abs(transforms[1][1] + Math.PI/2) < 1e-9, 'Preview should rotate the downward-facing source to the rightward route');
}
console.log('PASS preview: waypoint fill and outline centered on the actual curve at three widths');
