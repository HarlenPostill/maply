import L from 'leaflet';

// Geoman expects a global `L`; this module must be imported before it.
(window as unknown as { L: typeof L }).L = L;
