// Exercise category/search/pagination/selection with the real picker, without screen access.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '..');
const html = fs.readFileSync(path.join(root, 'ui_static/index.html'), 'utf8');
const source = html.slice(html.indexOf('const EMOJI_CATEGORY_RU ='), html.indexOf('\nfunction endpointImageField('));
const catalog = JSON.parse(fs.readFileSync(path.join(root, 'assets/emoji/catalog.json'), 'utf8'));
const defaults = JSON.parse(fs.readFileSync(path.join(root, 'config.json'), 'utf8'));
const nodes = new Map();
let activeElement, requestResolve;
const mk = (tag, props = {}) => {
  const node = {tag, ...props, children: [], attrs: {}, style: {}, value: '', hidden: false, isConnected: true,
    append(...children) { this.children.push(...children); },
    setAttribute(key, value) { this.attrs[key] = value; }, getAttribute(key) { return this.attrs[key]; },
    focus() { activeElement = this; }, addEventListener(key, callback) { this[key] = callback; },
    querySelectorAll() { return this.children; }};
  Object.defineProperty(node, 'innerHTML', {set() { this.children = []; }});
  return node;
};
const $ = selector => { if (!nodes.has(selector)) nodes.set(selector, mk('div')); return nodes.get(selector); };
const sandbox = {mk, $, cfg: defaults, emojiCatalog: null, lang: 'ru', TOKEN: 'test-token',
  t: (key, args) => key + (args ? args.n : ''),
  api: () => new Promise(resolve => { requestResolve = resolve; }),
  get: (obj, key) => key.split('.').reduce((value, part) => value[part], obj),
  set: (obj, key, value) => { const [group, field] = key.split('.'); obj[group][field] = value; },
  renderRedraw() {}, changed() {}, document: {get activeElement() { return activeElement; }}};
(async () => {
  vm.runInNewContext(source, sandbox);
  const total = catalog.groups.reduce((n, group) => n + group.emojis.length, 0);
  assert(total > 3000);
  assert.equal(sandbox.filterEmojis(catalog, '', '').length, total);
  assert(sandbox.filterEmojis(catalog, '', 'кофе').length > 0);
  assert(sandbox.filterEmojis(catalog, '', 'motorcycle').length > 0);
  assert(sandbox.filterEmojis(catalog, '', '👩🏽‍💻').length > 0);
  const people = catalog.groups.find(group => group.name === 'People & Body');
  assert.equal(sandbox.filterEmojis(catalog, people.name, '').length, people.emojis.length);
  const opening = sandbox.openEmojiPicker('follow_settings.end_emoji', mk('button'));
  assert.equal($('#emoji-picker').hidden, false);
  assert.equal($('#emoji-count').textContent, 'emoji_loading');
  requestResolve(catalog); await opening;
  assert($('#emoji-grid').children.length > 0);
  $('#emoji-categories').children[0].onclick(); // All categories.
  assert.equal($('#emoji-grid').children.length, 120);
  $('#emoji-more').onclick(); assert.equal($('#emoji-grid').children.length, 240);
  $('#emoji-search').value = 'coffee'; $('#emoji-search').oninput();
  const first = $('#emoji-grid').children[0]; assert(first);
  assert(first.children[0].src.startsWith('/api/emoji?value='));
  const selected = decodeURIComponent(first.children[0].src.split('value=')[1].split('&')[0]);
  first.onclick(); assert.equal(defaults.follow_settings.end_emoji, selected);
  assert.equal(defaults.follow_settings.start_emoji, 'green_flag');
  assert.equal($('#emoji-picker').hidden, true);
  console.log(`PASS picker: ${total} entries, category grid, Russian/English search, pagination, independent selection, first-load feedback`);
})().catch(error => { console.error(error); process.exitCode = 1; });
