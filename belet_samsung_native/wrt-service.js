import { Service } from 'wrt:service';
window.__neonWrtService = Service;
window.dispatchEvent(new Event('neon-wrt-ready'));
