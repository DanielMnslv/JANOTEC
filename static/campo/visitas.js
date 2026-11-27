(() => {
  const key = 'janotec-visitas-campo';
  const validity = 3 * 24 * 60 * 60 * 1000;
  const privateCache = 'janotec-actas-preparadas';
  const scriptUrl = document.currentScript.src;
  const list = document.querySelector('[data-campo-fichas]');
  const account = document.body.dataset.usuario;

  function read() {
    const saved = JSON.parse(localStorage.getItem(key) || 'null');
    if (!saved) return { account, visits: [] };
    if (!list && saved.account !== account) {
      localStorage.removeItem(key);
      window.caches?.delete(privateCache).catch(() => {});
      return { account, visits: [] };
    }
    const visits = saved.visits.filter(visit => Date.now() - visit.savedAt < validity);
    if (visits.length !== saved.visits.length) {
      write({ ...saved, visits });
      const expired = saved.visits.filter(visit => !visits.includes(visit) && visit.offlineActa);
      window.caches?.open(privateCache).then(cache =>
        Promise.all(expired.map(visit => cache.delete(visit.offlineActa)))
      ).catch(() => {});
    }
    return { ...saved, visits };
  }

  function write(saved) {
    if (saved.visits.length) localStorage.setItem(key, JSON.stringify(saved));
    else localStorage.removeItem(key);
  }

  function addText(parent, tag, value) {
    const element = document.createElement(tag);
    element.textContent = value;
    parent.append(element);
    return element;
  }

  function showVisits() {
    list.replaceChildren();
    try {
      const saved = read();
      if (!saved.visits.length) addText(list, 'p', 'No hay visitas guardadas en este dispositivo.');
      for (const visit of saved.visits) {
        const card = document.createElement('article');
        addText(card, 'h2', visit.asunto);
        addText(card, 'p', visit.radicado);
        addText(card, 'p', [visit.cliente, visit.sede].filter(Boolean).join(' · '));
        if (visit.direccion) addText(card, 'p', visit.direccion);
        if (visit.contacto || visit.telefono) {
          addText(card, 'p', [visit.contacto, visit.telefono].filter(Boolean).join(' · '));
        }
        addText(card, 'p', 'Guardada el ' + new Date(visit.savedAt).toLocaleString('es-CO'));
        const ticketUrl = new URL(visit.ticket, location.origin);
        if (visit.offlineActa) {
          const actaUrl = new URL(visit.offlineActa, location.origin);
          if (actaUrl.origin === location.origin) addText(card, 'a', 'Diligenciar acta').href = actaUrl.href;
        }
        if (ticketUrl.origin === location.origin) addText(card, 'a', 'Abrir ticket en línea').href = ticketUrl.href;
        const remove = addText(card, 'button', 'Quitar del dispositivo');
        remove.type = 'button';
        remove.addEventListener('click', () => {
          try {
            const current = read();
            write({ ...current, visits: current.visits.filter(item => item.radicado !== visit.radicado) });
            if (visit.offlineActa) window.caches?.open(privateCache).then(cache => cache.delete(visit.offlineActa)).catch(() => {});
            showVisits();
          } catch {
            addText(card, 'p', 'El navegador no permitió quitar la visita.');
          }
        });
        list.append(card);
      }
    } catch {
      addText(list, 'p', 'El navegador no permitió leer las visitas guardadas.');
    }
  }

  if (list) {
    const home = new URL('../../', scriptUrl);
    document.querySelector('[data-campo-inicio]').href = home.href;
    showVisits();
    try {
      const saved = read();
      if (saved.account) {
        document.body.dataset.usuario = saved.account;
        const queue = document.createElement('script');
        queue.src = new URL('envios.js', scriptUrl).href;
        document.body.append(queue);
      }
    } catch { /* almacenamiento bloqueado */ }
    window.addEventListener('pageshow', showVisits);
    window.addEventListener('storage', event => {
      if (event.key === key || event.key === null) showVisits();
    });
    return;
  }

  const links = document.querySelectorAll('[data-campo-lista]');
  function updateLinks() {
    try {
      const hasVisits = read().visits.length > 0;
      links.forEach(link => { link.hidden = !hasVisits; });
    } catch {
      links.forEach(link => { link.hidden = true; });
    }
  }
  updateLinks();
  window.addEventListener('pageshow', updateLinks);
  document.querySelector('[data-campo-salir]')?.addEventListener('submit', async event => {
    event.preventDefault();
    const logout = event.currentTarget;
    try { localStorage.removeItem(key); } catch { /* almacenamiento bloqueado */ }
    try { await window.caches?.delete(privateCache); } finally { logout.submit(); }
  });

  const prepare = document.querySelector('[data-campo-preparar]');
  if (!prepare || !account) return;
  const status = document.querySelector('[data-campo-estado]');
  prepare.addEventListener('click', async () => {
    if (!navigator.serviceWorker) {
      status.textContent = 'Para preparar visitas, abra JANOTEC por HTTPS en un navegador compatible.';
      return;
    }
    prepare.disabled = true;
    status.textContent = 'Preparando visita…';
    try {
      const registration = await navigator.serviceWorker.register(new URL('../../campo-sw.js', scriptUrl));
      const worker = registration.active || registration.waiting || registration.installing;
      if (!worker) throw new Error('Service worker no disponible');
      await new Promise((resolve, reject) => {
        function check() {
          if (worker.state === 'activated') resolve();
          if (worker.state === 'redundant') reject(new Error('Service worker no disponible'));
        }
        worker.addEventListener('statechange', check);
        check();
      });
      const saved = read();
      const visit = { ...prepare.dataset, savedAt: Date.now() };
      delete visit.campoPreparar;
      const actaUrl = new URL(visit.acta, location.origin);
      actaUrl.searchParams.set('campo', crypto.randomUUID());
      const acta = await fetch(actaUrl, { cache: 'no-store' });
      if (!acta.ok || acta.redirected) throw new Error('Acta no disponible');
      const html = await acta.text();
      const page = new DOMParser().parseFromString(html, 'text/html');
      if (page.body.dataset.usuario !== account || !page.querySelector('[data-campo-acta]')) {
        throw new Error('La cuenta o el acta cambió');
      }
      const staticRoot = new URL('../', scriptUrl).href;
      const resources = [...page.querySelectorAll('script[src], link[rel=stylesheet][href]')]
        .map(element => new URL(element.getAttribute('src') || element.getAttribute('href'), actaUrl).href)
        .filter(url => url.startsWith(staticRoot));
      const cache = await caches.open(privateCache);
      await cache.addAll([...new Set(resources)]);
      await cache.put(actaUrl, new Response(html, {
        headers: {
          'Content-Type': 'text/html; charset=utf-8',
          'X-Janotec-Vence': String(visit.savedAt + validity),
        },
      }));
      const previous = saved.visits.find(item => item.radicado === visit.radicado);
      visit.offlineActa = actaUrl.href;
      saved.visits = [visit, ...saved.visits.filter(item => item.radicado !== visit.radicado)];
      write(saved);
      if (previous?.offlineActa) await cache.delete(previous.offlineActa);
      updateLinks();
      status.textContent = 'Visita guardada. Puede consultarla sin conexión en Visitas guardadas.';
    } catch {
      status.textContent = 'No se pudo preparar la visita. Revise la conexión y el almacenamiento del navegador.';
    } finally {
      prepare.disabled = false;
    }
  });
})();
