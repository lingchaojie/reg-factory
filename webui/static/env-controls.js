'use strict';

(function(root){
  function isBooleanChecked(value){
    return ['1','true','yes','on'].includes(
      String(value ?? '').trim().toLowerCase()
    );
  }

  function renderBooleanControl(key, value){
    return `<input type="checkbox" data-env="${key}" ${isBooleanChecked(value)?'checked':''}>`;
  }

  function controlValue(control){
    return control.type === 'checkbox'
      ? (control.checked ? 'true' : 'false')
      : control.value;
  }

  function collect(controls, includeEmpty){
    const env = {};
    Array.from(controls).forEach(control=>{
      const value = controlValue(control);
      if(includeEmpty || value !== '') env[control.dataset.env] = value;
    });
    return env;
  }

  const api = {
    renderBooleanControl,
    collectForConnectionTest: controls=>collect(controls, false),
    collectForSave: controls=>collect(controls, true),
  };

  if(typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.EnvControls = api;
})(typeof globalThis !== 'undefined' ? globalThis : this);
