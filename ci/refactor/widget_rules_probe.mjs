/** Execute the pinned native widget engine against effective metadata. */
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {pathToFileURL} from 'node:url';
import {dirname, resolve} from 'node:path';
import {createHash} from 'node:crypto';
const [metadataPath, modulePath] = process.argv.slice(2);
assert(metadataPath && modulePath, 'Usage: node widget_rules_probe.mjs <target-pass-1.json> <native registry-widgets/dist/index.esm.js>');
const bytes = readFileSync(metadataPath);
const metadata = JSON.parse(bytes).metadata;
const native = await import(pathToFileURL(resolve(modulePath)).href);
const version = JSON.parse(readFileSync(resolve(dirname(modulePath), '../package.json'), 'utf8')).version;
const language = metadata.registry_languages.find(row => row.is_default);
assert(language);
const messages = typeof language.domain_translation === 'string' ? JSON.parse(language.domain_translation) : language.domain_translation;
function* walk(node) {
  if (Array.isArray(node)) { for (const value of node) yield* walk(value); }
  else if (node && typeof node === 'object') {
    if (node.widget) yield node;
    for (const value of Object.values(node)) yield* walk(value);
  }
}
const widgets = [...walk(metadata.g2p_register_sections)];
const field = widget => widget['column-key'] || (typeof widget['widget-data-path'] === 'string' ? widget['widget-data-path'].split('.').at(-1) : widget['widget-id']);
const cases = {
  farmer_id: [['FR-000000000001', true], [' FR-000000000001 ', true], ['FR-123', false], ['fr-000000000001', false], ['FR-١٢٣٤٥٦٧٨٩٠١٢', false]],
  fayda_fin: [['001234567890', true], [' 001234567890 ', true], ['0000000000000001', false], ['0012AB567890', false]],
  fayda_fan_id: [['0000000000000001', true], ['0000 0000 0000 0001', true], ['001234567890', false], ['000000000000000A', false]],
};
let assertions = 0, identityWidgets = 0, nameWidgets = 0, dateBounds = 0, alternatives = 0;
for (const widget of widgets) {
  const key = field(widget);
  const validation = widget['widget-data-validation'];
  if (widget['widget-readonly'] !== true && cases[key]) {
    assert(validation?.pattern && validation.patternMessage);
    assert(messages[validation.patternMessage] && !messages[validation.patternMessage].includes('{label}'));
    for (const [value, valid] of cases[key]) {
      const errors = native.validateWidget(value, validation, false);
      assert.equal(errors.length === 0, valid, `${key}: ${value}`);
      if (!valid) assert(errors.includes(validation.patternMessage));
      assertions++;
    }
    identityWidgets++;
  }
  if (validation?.patternMessage?.startsWith('coss_input_') && /name/.test(key) && widget['widget-readonly'] !== true) {
    assert(messages[validation.patternMessage] && !messages[validation.patternMessage].includes('{label}'));
    for (const [value, valid] of [['Abebe', !key.endsWith('_amh')], ['አበበ', true], ["O'Neil", !key.endsWith('_amh')], ['Farmer123', false], ['Farmer_Name', false], ['👩', false], ['x'.repeat(101), false]]) {
      assert.equal(native.validateWidget(value, validation, false).length === 0, valid, `${key}: name rule`);
      assertions++;
    }
    nameWidgets++;
  }
  if (widget.widget === 'date' && widget['widget-data-options']?.minDate === '1900-01-01') {
    assert(messages[widget['widget-data-options'].minDateMessage]); dateBounds++;
  }
  if (key === 'fayda_fan_id' && widget['widget-data-options']?.actions?.some(rule => rule.action === 'require')) {
    const options = widget['widget-data-options'];
    const condition = options.actions.find(rule => rule.action === 'require').condition;
    assert.equal(condition.operator, 'empty');
    const [root, attribute] = condition.field.split('.');
    for (const value of [undefined, null, '', '001234567890']) {
      assert.equal(native.evaluateWidgetConditions(options, {[root]: {[attribute]: value}}, false).required, !value);
      assertions++;
    }
    alternatives++;
  }
}
assert(identityWidgets >= 3 && nameWidgets >= 2 && dateBounds >= 2 && alternatives >= 1);
console.log(JSON.stringify({native_widget_version: version, metadata_sha256: createHash('sha256').update(bytes).digest('hex'), assertions, identity_widgets: identityWidgets, name_widgets: nameWidgets, date_bound_metadata: dateBounds, conditional_fan_widgets: alternatives, native_engine_result: 'PASS', browser_blur_next_and_cr: 'NOT RUN', registry_runtime_changed: false}));
