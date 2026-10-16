// Leaflet-Geoman (free) is an IIFE that expects a global `L`. Leaflet's UMD
// build already assigns window.L, but we make it explicit so module
// evaluation order (this file is imported before geoman) is guaranteed.
import L from 'leaflet';

(window as unknown as { L: typeof L }).L = L;

export default L;
