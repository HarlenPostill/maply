const { test } = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');

// Load the actual content script and capture its camera feedback function.
function parseCamera(url) {
  const source = fs.readFileSync(require.resolve('../content.js'), 'utf8')
    .replace('  function metres(a, b)', '  globalThis.cameraParser = parseView;\n  function metres(a, b)');
  const context = vm.createContext({ URL, window: { addEventListener() {} },
    chrome: { runtime: { sendMessage: async () => ({ active: false }), onMessage: { addListener() {} } } } });
  vm.runInContext(source, context);
  return context.cameraParser(url);
}

for (const mode of ['3a', '15a', '16a']) {
  test(`camera feedback recognises ${mode} panorama URLs`, () => {
    const view = parseCamera(`https://www.google.com/maps/@-33.90308,151.1912439,${mode},80y,311.2h,82t/data=!3m7!1e1!3m5!1sPANO!2e0`);
    assert.equal(view.heading, 311.2);
    assert.equal(view.fov, 80);
    assert.equal(view.pitch, -8);
  });
}
test('aerial maps do not receive panorama camera feedback', () => {
  assert.equal(parseCamera('https://www.google.com/maps/@-33.90308,151.1912439,16a,80y,311.2h,82t'), null);
});
