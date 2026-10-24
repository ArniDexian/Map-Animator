// Exercise the real style selection handlers without a browser or screen access.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '..');
const html = fs.readFileSync(path.join(root, 'ui_static/index.html'), 'utf8');
const fields = html.slice(html.indexOf('const ENDPOINT_ICONS ='), html.indexOf('const REDRAW_TAIL ='));
const normalize = html.slice(html.indexOf('function normalizeEndpointChoices('), html.indexOf('function renderRedraw()'));
const styles = html.slice(html.indexOf('const STYLE_KEYS ='), html.indexOf('let previewBg ='));
const defaults = JSON.parse(fs.readFileSync(path.join(root, 'config.json'), 'utf8'));
const nodes = new Map();
const node = id => {
  if (!nodes.has(id)) nodes.set(id, {value: '', addEventListener() {}, select() {}, focus() {}, append() {}});
  return nodes.get(id);
};
const sandbox = {assert, defaults, cfg: JSON.parse(JSON.stringify(defaults)), styles: [],
  $: node, t: key => key, renderRedraw() {}, changed() {}, save() {},
  setTimeout() {}, clearTimeout() {}, mk() { return {}; }};
vm.runInNewContext(fields + '\n' + normalize + '\n' + styles + `
renderStyles = () => {};
cfg.mode = 'follow';
cfg.follow_settings.hide_waypoints = true;
cfg.follow_settings.start_icon = 'custom';
cfg.follow_settings.start_image = 'data:image/png;base64,fixture';
cfg.follow_settings.end_icon = 'emoji';
cfg.follow_settings.end_emoji = '🌳';
cfg.follow_settings.end_always_visible = true;
cfg.follow_settings.start_offset_x_px = -15;
cfg.follow_settings.end_offset_y_px = 12;
cfg.follow_settings.start_size_px = 48;
const savedStyle = JSON.parse(JSON.stringify(pickStyle()));
styles.push({name: 'Custom endpoints', mode: 'follow', settings: savedStyle});
cfg = JSON.parse(JSON.stringify(defaults)); cfg.mode = 'follow';
$('#style-select').onchange({target: {value: 'Custom endpoints'}});
assert.equal(cfg.follow_settings.hide_waypoints, true);
assert.equal(cfg.follow_settings.start_icon, 'custom');
assert.equal(cfg.follow_settings.start_image, savedStyle.start_image);
assert.equal(cfg.follow_settings.start_size_px, 48);
assert.equal(cfg.follow_settings.end_icon, 'emoji');
assert.equal(cfg.follow_settings.end_emoji, '🌳');
assert.equal(cfg.follow_settings.end_always_visible, true);
assert.equal(cfg.follow_settings.start_offset_x_px, -15);
assert.equal(cfg.follow_settings.end_offset_y_px, 12);
assert.equal(findStyle().name, 'Custom endpoints');
const legacy = Object.fromEntries(['line_color_bgr', 'line_thickness', 'waypoint_color_bgr', 'waypoint_outline_color_bgr', 'waypoint_diameter_px', 'waypoint_outline_px'].map(k => [k, savedStyle[k]]));
styles.push({name: 'Legacy follow', mode: 'follow', settings: legacy});
$('#style-select').onchange({target: {value: 'Legacy follow'}});
assert.equal(cfg.follow_settings.hide_waypoints, false);
assert.equal(cfg.follow_settings.start_icon, 'emoji');
assert.equal(cfg.follow_settings.start_emoji, 'green_flag');
assert.equal(cfg.follow_settings.start_image, null);
assert.equal(cfg.follow_settings.end_icon, 'emoji');
assert.equal(cfg.follow_settings.end_emoji, '🏁');
assert.equal(cfg.follow_settings.end_always_visible, false);
assert.equal(cfg.follow_settings.start_offset_x_px, 0);
assert.equal(cfg.follow_settings.end_offset_y_px, 0);
styles.push({name: 'Old flags', mode: 'follow', settings: {...legacy, start_icon: 'pin', end_icon: 'checkered_flag'}});
$('#style-select').onchange({target: {value: 'Old flags'}});
assert.equal(cfg.follow_settings.start_icon, 'emoji');
assert.equal(cfg.follow_settings.start_emoji, '📍');
assert.equal(cfg.follow_settings.end_icon, 'emoji');
assert.equal(cfg.follow_settings.end_emoji, '🏁');
assert.equal(ENDPOINT_ICONS.length, 3);
assert.equal(ENDPOINT_ICONS.map(item => item[0]).join(','), 'emoji,none,custom');
assert(FOLLOW.some(field => field.p === 'follow_settings.end_always_visible' && field.t === 'bool'));
assert(FOLLOW.findIndex(field => field.p === 'follow_settings.end_always_visible') >
  FOLLOW.findIndex(field => field.p === 'follow_settings.end_image'),
  'always-show control should come after finish image selection');
cfg.mode = 'redraw';
assert.equal(currentStyles().length, 0);
`, sandbox);
assert(html.includes('f_endalways: "Всегда показывать финиш"'), 'Russian interface label is missing');
assert(html.includes('f_endalways: "Always show finish"'), 'English interface label is missing');
assert(fs.readFileSync(path.join(root, 'README.md'), 'utf8').includes('**Always show finish**'), 'English documentation is outdated');
assert(fs.readFileSync(path.join(root, 'README.ru.md'), 'utf8').includes('**«Всегда показывать финиш»**'), 'Russian documentation is outdated');
console.log('PASS styles: hidden waypoints, independent icons, image data, sizes, legacy defaults and mode isolation');
