'use strict';

const assert = require('node:assert/strict');
const NexaCardWebUi = require('../webui/static/nexacard-webui.js');

function target(){
  const classes = new Set();
  return {
    textContent: '',
    classList: {
      add: (...names) => names.forEach(name => classes.add(name)),
      remove: (...names) => names.forEach(name => classes.delete(name)),
      toggle: (name, active) => active ? classes.add(name) : classes.delete(name),
      contains: name => classes.has(name),
    },
  };
}

assert.equal(NexaCardWebUi.shouldRenderGoogleOauthActions({gmail_oauth: true}), true);
assert.equal(NexaCardWebUi.shouldRenderGoogleOauthActions({gmail_oauth: false}), false);
assert.equal(NexaCardWebUi.shouldRenderGoogleOauthActions({}), false);

assert.deepEqual(
  NexaCardWebUi.envInputMetadata({type: 'number'}),
  {type: 'number', min: '0.000001', step: 'any'},
);
assert.deepEqual(
  NexaCardWebUi.envInputMetadata({type: 'int'}),
  {type: 'number', min: '1', step: '1'},
);
assert.deepEqual(
  NexaCardWebUi.envInputMetadata({secret: true}),
  {type: 'password'},
);

async function testLatestStatusResponseWins(){
  let email = 'old@example.com';
  const status = target();
  const pending = [];
  const fetch = () => new Promise(resolve => pending.push(resolve));

  const oldRequest = NexaCardWebUi.loadOauthStatus(email, status, () => email, fetch);
  email = 'new@example.com';
  const newRequest = NexaCardWebUi.loadOauthStatus(email, status, () => email, fetch);
  pending[1]({ok: true, json: async () => ({state: 'valid', message: 'new status', authorized_email: email})});
  await newRequest;
  pending[0]({ok: true, json: async () => ({state: 'reauthorize', message: 'old status', authorized_email: 'old@example.com'})});
  await oldRequest;

  assert.equal(status.textContent, 'new status · new@example.com');
  assert.equal(status.classList.contains('bad'), false);
}

async function testCurrentEmailMustStillMatch(){
  let email = 'old@example.com';
  const status = target();
  let resolve;
  const request = NexaCardWebUi.loadOauthStatus(email, status, () => email, () => new Promise(done => { resolve = done; }));
  email = 'changed@example.com';
  resolve({ok: true, json: async () => ({state: 'reauthorize', message: 'stale', authorized_email: 'old@example.com'})});
  await request;

  assert.notEqual(status.textContent, 'stale · old@example.com');
  assert.equal(status.classList.contains('bad'), false);
}

Promise.all([testLatestStatusResponseWins(), testCurrentEmailMustStillMatch()])
  .then(() => console.log('NexaCard WebUI behavior tests passed'))
  .catch(error => { console.error(error); process.exitCode = 1; });
