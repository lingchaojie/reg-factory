'use strict';

const assert = require('node:assert/strict');
const EnvControls = require('../webui/static/env-controls.js');

function rendersChecked(value){
  const html = EnvControls.renderBooleanControl('OCTO_HEADLESS', value);
  assert.match(html, /type="checkbox"/);
  return /\schecked(?:\s|>)/.test(html);
}

for(const value of ['1', 'true', 'yes', 'on', ' 1 ', ' TRUE ', '\tYeS\n', ' On ']){
  assert.equal(rendersChecked(value), true, `${JSON.stringify(value)} should be checked`);
}

for(const value of ['false', '', 'invalid', '0', 'off', 'no', false, null, undefined]){
  assert.equal(rendersChecked(value), false, `${JSON.stringify(value)} should be unchecked`);
}

const controls = [
  {type: 'checkbox', checked: true, dataset: {env: 'CHECKED'}},
  {type: 'checkbox', checked: false, dataset: {env: 'UNCHECKED'}},
  {type: 'text', value: '', dataset: {env: 'EMPTY_TEXT'}},
  {type: 'text', value: 'value', dataset: {env: 'TEXT'}},
];

assert.deepEqual(EnvControls.collectForConnectionTest(controls), {
  CHECKED: 'true',
  UNCHECKED: 'false',
  TEXT: 'value',
});
assert.deepEqual(EnvControls.collectForSave(controls), {
  CHECKED: 'true',
  UNCHECKED: 'false',
  EMPTY_TEXT: '',
  TEXT: 'value',
});

for(const collect of [
  EnvControls.collectForConnectionTest,
  EnvControls.collectForSave,
]){
  const env = collect(controls);
  assert.equal(typeof env.CHECKED, 'string');
  assert.equal(typeof env.UNCHECKED, 'string');
}

console.log('WebUI environment control behavior tests passed');
