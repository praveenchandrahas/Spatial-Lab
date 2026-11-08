# Vendored libraries

Kept in the repository so the page works with no internet access.

- `three.module.js`: three.js r160, `build/three.module.min.js` from the npm package `three@0.160.1`, unmodified.
- `OrbitControls.js`: `examples/jsm/controls/OrbitControls.js` from the same package. One change: the bare
  import `from 'three'` is rewritten to `from './three.module.js'`, so no import map (and no inline script) is needed.
- `LICENSE-three.txt`: the MIT licence of three.js.
