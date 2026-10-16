import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import './lib/map'; // Leaflet + Geoman + their CSS (bundled, no CDN)
import './styles.css';
import { App } from './App';

const el = document.getElementById('root');
if (!el) throw new Error('#root missing');
createRoot(el).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
