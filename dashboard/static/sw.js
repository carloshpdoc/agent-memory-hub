// Network-only on purpose: o mesada-painel teve cache preso servindo versao velha.
// Existe so para o app ser instalavel; nao guarda nada.
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (e) => e.waitUntil(self.clients.claim()));
self.addEventListener('fetch', () => {});
