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

function control(key, value, valid){
  return {
    dataset: {env: key},
    value,
    checkValidity: () => valid,
    reportValidityCalls: 0,
    reportValidity(){ this.reportValidityCalls += 1; },
  };
}

async function testInvalidConfigurationNeverPosts(){
  const message = target();
  const interval = control('NEXACARD_OTP_POLL_INTERVAL_SECONDS', '0', false);
  const attempts = control('NEXACARD_OTP_MAX_ATTEMPTS', '1.5', false);
  let fetchCalls = 0;
  const result = await NexaCardWebUi.saveEnvControls(
    [interval, attempts], message, async () => { fetchCalls += 1; },
  );

  assert.equal(result, null);
  assert.equal(fetchCalls, 0);
  assert.equal(interval.reportValidityCalls, 1);
  assert.equal(attempts.reportValidityCalls, 0);
  assert.equal(message.textContent, '请修正无效配置');
}

async function testValidDecimalIntervalAndIntegerAttemptsPost(){
  const message = target();
  let request;
  const result = await NexaCardWebUi.saveEnvControls(
    [
      control('NEXACARD_OTP_POLL_INTERVAL_SECONDS', '4.5', true),
      control('NEXACARD_OTP_MAX_ATTEMPTS', '100', true),
    ],
    message,
    async (url, options) => {
      request = {url, options};
      return {json: async () => ({ok: true, saved: 2})};
    },
  );

  assert.deepEqual(result, {ok: true, saved: 2});
  assert.equal(request.url, '/api/env');
  assert.deepEqual(JSON.parse(request.options.body), {
    env: {
      NEXACARD_OTP_POLL_INTERVAL_SECONDS: '4.5',
      NEXACARD_OTP_MAX_ATTEMPTS: '100',
    },
  });
}

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

Promise.all([
  testLatestStatusResponseWins(),
  testCurrentEmailMustStillMatch(),
  testInvalidConfigurationNeverPosts(),
  testValidDecimalIntervalAndIntegerAttemptsPost(),
])
  .then(() => console.log('NexaCard WebUI behavior tests passed'))
  .catch(error => { console.error(error); process.exitCode = 1; });
