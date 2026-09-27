// Loaded only in Saygo's private Windows Appium process. Appium's adb/logcat
// helpers otherwise open console windows when started by a detached Node host.
const cp = require('node:child_process');
for (const name of ['spawn', 'spawnSync']) {
  const original = cp[name];
  cp[name] = function (command, args, options) {
    if (!Array.isArray(args)) {
      options = args;
      args = [];
    }
    return original.call(this, command, args, { ...options, windowsHide: true });
  };
}
require('node:module').syncBuiltinESMExports();
