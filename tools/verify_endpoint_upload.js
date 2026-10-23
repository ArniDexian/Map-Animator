// Verify image upload normalization and validation without browser/screen access.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '..');
const html = fs.readFileSync(path.join(root, 'ui_static/index.html'), 'utf8');
const source = html.slice(html.indexOf('function endpointImageField('), html.indexOf('\nfunction field('));
const canvases = [];
const mk = (tag, props) => ({tag, ...props, children: [], style: {}, append(...kids) { this.children.push(...kids); }});
const cfg = {follow_settings: {start_image: null}};
const sandbox = {cfg, mk, t: key => key,
  labelWrap: (label, input) => { const box = mk('label'); box.append(input); return box; },
  set: (obj, key, value) => { const [section, field] = key.split('.'); obj[section][field] = value; },
  renderRedraw() {}, changed() {},
  FileReader: class { readAsDataURL() { this.result = 'data:image/png;base64,source'; this.onload(); } },
  Image: class { set src(value) { this.naturalWidth = 800; this.naturalHeight = 400; this.onload(); } },
  document: {createElement(tag) {
    assert.equal(tag, 'canvas');
    const canvas = {getContext: () => ({drawImage() {}}), toDataURL: () => 'data:image/png;base64,normalized'};
    canvases.push(canvas); return canvas;
  }},
};
(async () => {
  vm.runInNewContext(source, sandbox);
  const box = vm.runInNewContext('endpointImageField({p:"follow_settings.start_image", l:"f_iconimage"}, null)', sandbox);
  const input = box.children[0], note = box.children[1];
  input.files = [{type: 'image/png', size: 1024}];
  await input.onchange();
  assert.equal(canvases[0].width, 256); assert.equal(canvases[0].height, 128);
  assert.equal(cfg.follow_settings.start_image, 'data:image/png;base64,normalized');
  for (const file of [{type: 'image/png', size: 3 * 1024 * 1024}, {type: 'image/svg+xml', size: 100}]) {
    cfg.follow_settings.start_image = 'previous'; input.files = [file]; await input.onchange();
    assert.equal(note.textContent, 'icon_image_error');
    assert.equal(cfg.follow_settings.start_image, 'previous');
  }
  console.log('PASS uploads: bounded image, aspect-preserving resize, embedded data, invalid uploads keep previous image');
})().catch(error => { console.error(error); process.exitCode = 1; });
