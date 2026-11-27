(() => {
  const account = document.body.dataset.usuario;
  if (!account || document.body.hasAttribute('data-campo-prueba') || !window.indexedDB || !crypto.randomUUID) return;
  const status = document.querySelector('[data-campo-envios]');
  const form = document.querySelector('[data-campo-acta]');
  let syncing = false;
  let saving = false;

  function operation(mode, execute) {
    return new Promise((resolve, reject) => {
      const opening = indexedDB.open('janotec-envios-campo', 1);
      opening.onupgradeneeded = () => opening.result.createObjectStore('envios', { keyPath: 'key' });
      opening.onerror = () => reject(opening.error);
      opening.onsuccess = () => {
        const db = opening.result;
        const tx = db.transaction('envios', mode);
        const request = execute(tx.objectStore('envios'));
        tx.oncomplete = () => { db.close(); resolve(request.result); };
        tx.onabort = () => { db.close(); reject(tx.error); };
      };
    });
  }

  const all = async () => (await operation('readonly', store => store.getAll())).filter(item => item.account === account);
  const update = (item, callback) => operation('readwrite', store => {
    const request = store.get(item.key);
    request.onsuccess = () => {
      if (request.result?.revision === item.revision) callback(store);
    };
    return request;
  });

  function show(message, error = false) {
    if (!status) return;
    status.className = 'alert ' + (error ? 'alert-warning' : 'alert-info');
    status.textContent = message;
    status.hidden = !message;
  }

  function pending() {
    show('Acta guardada en el dispositivo. Pendiente de envío.');
    if (!status) return;
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'btn btn-sm btn-outline-primary ms-2';
    button.textContent = 'Sincronizar';
    button.addEventListener('click', synchronize);
    status.append(button);
  }

  function review(item) {
    show(item.error, true);
    if (form?.dataset.borrador === item.key || !status) return;
    const link = document.createElement('a');
    link.href = item.url;
    link.className = 'ms-2';
    link.textContent = 'Revisar acta';
    status.append(link);
  }

  async function synchronize() {
    if (syncing) return;
    syncing = true;
    try {
      for (const item of await all()) {
        if (item.error) {
          review(item);
          continue;
        }
        if (!navigator.onLine) {
          pending();
          break;
        }
        const payload = new FormData();
        for (const [name, value] of item.fields) payload.append(name, value);
        const csrf = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]*)/)?.[1];
        if (csrf) payload.set('csrfmiddlewaretoken', csrf);
        const response = await fetch(item.url, {
          method: 'POST',
          body: payload,
          headers: {
            'X-Janotec-Envio': item.id,
            'X-Janotec-Usuario': account,
            'X-Janotec-Visita': item.version,
          },
        });
        if (response.redirected || response.status === 403 || response.status === 404) {
          show('Abra nuevamente la cuenta y la visita para sincronizar el acta.', true);
          break;
        }
        if (response.status >= 500) throw new Error('Servidor no disponible');
        const result = await response.json();
        if (!response.ok) {
          item.error = result.error || 'El acta requiere revisión.';
          await update(item, store => store.put(item));
          review(item);
          continue;
        }
        await update(item, store => store.delete(item.key));
        if (form?.dataset.borrador === item.key) {
          form.dataset.campoRegistrada = '1';
          document.getElementById('confirmar-acta').disabled = true;
        }
        window.dispatchEvent(new CustomEvent('campo:acta-registrada', { detail: item.key }));
        show('Acta N.º ' + result.numero + ' registrada.');
        const link = document.createElement('a');
        link.href = result.url;
        link.className = 'ms-2';
        link.textContent = 'Ver acta';
        status?.append(link);
      }
    } catch {
      show('Envío pendiente. Se reintentará cuando vuelva la conexión.', true);
    } finally {
      syncing = false;
    }
  }

  window.addEventListener('online', synchronize);
  window.addEventListener('pageshow', synchronize);
  window.setInterval(synchronize, 30000);
  synchronize();
  if (!form) return;
  form.addEventListener('acta:confirmada', async event => {
    event.preventDefault();
    if (saving || form.dataset.campoRegistrada) return;
    saving = true;
    const key = form.dataset.borrador;
    const fields = [...new FormData(form)].filter(([, value]) => typeof value === 'string' || value.size);
    try {
      const saved = await operation('readonly', store => store.get(key));
      if (!saved && navigator.onLine && !new URL(location.href).searchParams.has('campo')) {
        form.dispatchEvent(new Event('acta:enviando'));
        form.submit();
        return;
      }
      await operation('readwrite', store => {
        const request = store.get(key);
        request.onsuccess = () => {
          if (request.result && !request.result.error) return;
          store.put({
            key, account, fields, url: form.dataset.campoActa, version: form.dataset.campoVersion,
            id: request.result?.id || form.dataset.campoIdentificador, revision: crypto.randomUUID(),
          });
        };
        return request;
      });
      form.dispatchEvent(new Event('campo:guardar-borrador'));
      pending();
      status?.scrollIntoView({ block: 'nearest' });
      await synchronize();
    } catch {
      show('El navegador no pudo guardar el envío. Mantenga el acta abierta y vuelva a intentarlo.', true);
    } finally {
      saving = false;
    }
  });
})();
