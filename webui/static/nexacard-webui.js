'use strict';

(function(root){
  const statusGenerations = new WeakMap();

  function shouldRenderGoogleOauthActions(item){
    return item?.gmail_oauth === true;
  }

  function envInputMetadata(item){
    if(item?.type === 'number') return {type: 'number', min: '0.000001', step: 'any'};
    if(item?.type === 'int') return {type: 'number', min: '1', step: '1'};
    return {type: item?.secret ? 'password' : 'text'};
  }

  function isCurrent(target, generation, requestedEmail, currentEmail){
    return statusGenerations.get(target) === generation
      && String(currentEmail()).trim() === requestedEmail;
  }

  function setStatus(target, text, state){
    target.textContent = text;
    target.classList.toggle('bad', state === 'reauthorize' || state === 'mismatch');
    target.classList.toggle('unknown', state === 'unknown');
  }

  async function loadOauthStatus(email, target, currentEmail, fetchImpl){
    const requestedEmail = String(email || '').trim();
    const generation = (statusGenerations.get(target) || 0) + 1;
    statusGenerations.set(target, generation);
    const current = currentEmail || (() => requestedEmail);
    if(!requestedEmail){
      if(isCurrent(target, generation, requestedEmail, current)) setStatus(target, '请先填写验证邮箱', '');
      return;
    }
    try{
      const fetcher = fetchImpl || root.fetch.bind(root);
      const response = await fetcher('/api/nexacard/oauth/status?email='+encodeURIComponent(requestedEmail));
      if(!response.ok) throw new Error('status request failed');
      const result = await response.json();
      if(!isCurrent(target, generation, requestedEmail, current)) return;
      const parts = [result.message || result.state || '授权状态未知'];
      if(result.authorized_email) parts.push(result.authorized_email);
      if(result.estimated_expires_at) parts.push(`${result.estimated ? '预计' : ''}到期 ${result.estimated_expires_at}`);
      setStatus(target, parts.join(' · '), result.state);
    }catch(error){
      if(!isCurrent(target, generation, requestedEmail, current)) return;
      setStatus(target, '暂时无法验证授权状态', 'unknown');
    }
  }

  const api = {shouldRenderGoogleOauthActions, envInputMetadata, loadOauthStatus};
  if(typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.NexaCardWebUi = api;
})(typeof globalThis !== 'undefined' ? globalThis : this);
