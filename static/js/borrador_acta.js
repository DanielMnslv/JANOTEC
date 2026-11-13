/* Borrador del acta de soporte (13/11/2026): mientras el técnico diligencia el acta, todo lo que escribe, marca,
   firma y las fotos que adjunta se guarda solo en este dispositivo (IndexedDB del navegador). Si entra una llamada,
   cambia de aplicación, se cierra la pestaña o se va el internet, al volver a abrir el acta se recupera tal cual.

   - El borrador es por usuario y por ticket (o por cliente y sede en el trabajo no programado).
   - Se guarda al escribir (cada segundo como máximo) y de inmediato cuando la página pasa a segundo plano.
   - Al guardar el acta con éxito (mensaje «Acta … registrada») el borrador se borra; si el servidor la devuelve con errores, se recuperan las
     fotos (el navegador no las conserva al recargar) y el borrador sigue.
   - Un borrador de más de 7 días se descarta.
   Este archivo se carga en todas las páginas (base.html): fuera del acta solo limpia el borrador ya enviado. */
(function () {
  const BD = 'janotec-borradores', ALMACEN = 'actas', ENVIADA = 'acta-enviada', VIGENCIA = 7 * 24 * 3600 * 1000;
  if (!window.indexedDB) return;

  function abrir() {
    return new Promise(function (ok, mal) {
      const p = indexedDB.open(BD, 1);
      p.onupgradeneeded = function () { p.result.createObjectStore(ALMACEN); };
      p.onsuccess = function () { ok(p.result); };
      p.onerror = function () { mal(p.error); };
    });
  }
  function operar(modo, fn) {
    return abrir().then(function (bd) {
      return new Promise(function (ok, mal) {
        const tx = bd.transaction(ALMACEN, modo), pedido = fn(tx.objectStore(ALMACEN));
        tx.oncomplete = function () { ok(pedido && pedido.result); bd.close(); };
        tx.onerror = tx.onabort = function () { mal(tx.error); bd.close(); };
      });
    });
  }
  const leer = function (clave) { return operar('readonly', function (s) { return s.get(clave); }); };
  const escribir = function (clave, v) { return operar('readwrite', function (s) { return s.put(v, clave); }); };
  const borrar = function (clave) { return operar('readwrite', function (s) { return s.delete(clave); }); };

  let enviada = null;
  try { enviada = sessionStorage.getItem(ENVIADA); } catch (e) { /* navegador sin almacenamiento de sesión */ }
  const form = document.querySelector('form[data-borrador]');

  // Se envió el acta y el servidor confirma que quedó registrada: el borrador sobra. Si la página es otra (un
  // error del servidor, sin internet), el borrador se conserva para volver a intentarlo.
  if (enviada && !form) {
    try { sessionStorage.removeItem(ENVIADA); } catch (e) { /* nada */ }
    if (document.querySelector('[data-acta-guardada]')) borrar(enviada).catch(function () {});
  }
  if (!form) return;

  const clave = form.dataset.borrador, reenviado = form.dataset.reenviado === '1';
  try { sessionStorage.removeItem(ENVIADA); } catch (e) { /* nada */ }
  const OMITIR = /^(csrfmiddlewaretoken|latitud|longitud)$|-(TOTAL|INITIAL|MIN_NUM|MAX_NUM)_FORMS$/;
  const estado = document.getElementById('estado-borrador');
  const hora = function (t) { return new Date(t).toLocaleTimeString('es-CO', { hour: '2-digit', minute: '2-digit' }); };
  const fecha = function (t) { return new Date(t).toLocaleString('es-CO', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' }); };

  function campos() { return [...form.elements].filter(function (c) { return c.name && !OMITIR.test(c.name) && c.type !== 'submit' && c.type !== 'button'; }); }

  function tomar() {
    const datos = { guardado: Date.now(), valores: {}, archivos: {}, filas: {} };
    campos().forEach(function (c) {
      if (c.type === 'file') {
        if (c.files.length) datos.archivos[c.name] = [...c.files];
      } else if (c.type === 'checkbox' || c.type === 'radio') {
        datos.valores[c.name] = datos.valores[c.name] || [];
        if (c.checked) datos.valores[c.name].push(c.value);
      } else if (c.multiple && c.options) {
        datos.valores[c.name] = [...c.selectedOptions].map(function (o) { return o.value; });
      } else {
        datos.valores[c.name] = [c.value];
      }
    });
    form.querySelectorAll('input[name$="-TOTAL_FORMS"]').forEach(function (t) { datos.filas[t.name.replace(/-TOTAL_FORMS$/, '')] = parseInt(t.value, 10) || 0; });
    return datos;
  }

  let pendiente = null, listo = false;
  function guardar() {
    clearTimeout(pendiente); pendiente = null;
    if (!listo) return;
    const datos = tomar();
    escribir(clave, datos).then(function () {
      if (estado) estado.innerHTML = '<i class="bi bi-cloud-check text-success"></i> Borrador guardado en este dispositivo · ' + hora(datos.guardado);
    }).catch(function () {
      if (estado) estado.innerHTML = '<i class="bi bi-exclamation-triangle text-warning"></i> No se pudo guardar el borrador (memoria del navegador llena o bloqueada).';
    });
  }
  function programar() { if (!pendiente) pendiente = setTimeout(guardar, 1000); }

  function ponerArchivos(c, lista) {
    if (!lista || !lista.length || !window.DataTransfer) return;
    try {
      const dt = new DataTransfer();
      lista.forEach(function (f) { dt.items.add(f); });
      c.files = dt.files;
    } catch (e) { /* navegador antiguo: las fotos se vuelven a adjuntar */ }
  }

  function restaurar(datos) {
    // Primero las filas agregadas (equipos, materiales…) para que existan sus campos
    Object.keys(datos.filas || {}).forEach(function (prefijo) {
      const total = form.querySelector('[name="' + prefijo + '-TOTAL_FORMS"]');
      const boton = form.querySelector('.agregar-fila[data-prefijo="' + prefijo + '"]');
      let vueltas = 0;
      while (total && boton && parseInt(total.value, 10) < datos.filas[prefijo] && vueltas++ < 60) boton.click();
    });
    if (!reenviado) {
      campos().forEach(function (c) {
        const v = datos.valores[c.name];
        if (v === undefined || c.type === 'file') return;
        if (c.type === 'checkbox' || c.type === 'radio') c.checked = v.indexOf(c.value) !== -1;
        else if (c.multiple && c.options) [...c.options].forEach(function (o) { o.selected = v.indexOf(o.value) !== -1; });
        else c.value = v[0];
      });
    }
    campos().forEach(function (c) { if (c.type === 'file') ponerArchivos(c, (datos.archivos || {})[c.name]); });
    // Que la página se ponga al día: firmas dibujadas, listas abiertas, contadores y modalidad
    form.querySelectorAll('.firma-pad').forEach(function (caja) {
      const campo = document.getElementById(caja.dataset.campo);
      if (!campo || !campo.value) return;
      const det = caja.closest('details');
      if (det && !det.open) det.open = true;
      caja.dispatchEvent(new Event('firma:cargar'));
    });
    form.querySelectorAll('.grupo-equipos').forEach(function (g) {
      const lleno = [...g.querySelectorAll('input[type=text], input:not([type]), textarea')].some(function (c) { return c.value.trim(); })
        || g.querySelector('input[type=checkbox]:checked') || [...g.querySelectorAll('input[type=file]')].some(function (c) { return c.files.length; });
      const caja = g.querySelector('.collapse'), boton = g.querySelector('[data-bs-toggle=collapse]');
      if (lleno && caja && !caja.classList.contains('show')) { caja.classList.add('show'); if (boton) { boton.classList.remove('collapsed'); boton.setAttribute('aria-expanded', 'true'); } }
    });
    form.querySelectorAll('input[name$="-equipo"]').forEach(function (c) { c.dispatchEvent(new Event('input', { bubbles: true })); });
    form.querySelectorAll('input[type=radio]:checked').forEach(function (c) { c.dispatchEvent(new Event('change', { bubbles: true })); });
  }

  function aviso(datos) {
    const caja = document.createElement('div');
    caja.className = 'alert alert-info d-flex flex-wrap align-items-center gap-2';
    const fotos = Object.values(datos.archivos || {}).reduce(function (n, l) { return n + l.length; }, 0);
    caja.innerHTML = '<i class="bi bi-arrow-counterclockwise"></i><span class="me-auto"></span>'
      + '<button type="button" class="btn btn-sm btn-outline-secondary">Empezar de cero</button>';
    caja.querySelector('span').textContent = reenviado
      ? (fotos === 1 ? 'Se volvió a adjuntar la foto que había puesto.' : 'Se volvieron a adjuntar las ' + fotos + ' fotos que había puesto.')
      : 'Se recuperó lo que había diligenciado el ' + fecha(datos.guardado)
        + (fotos ? (fotos === 1 ? ', con 1 foto' : ', con ' + fotos + ' fotos') : '') + '. Revise y continúe.';
    caja.querySelector('button').addEventListener('click', function () {
      if (!confirm('¿Borrar lo diligenciado y empezar el acta de cero?')) return;
      listo = false;
      borrar(clave).finally(function () { location.replace(location.pathname + location.search); });
    });
    if (reenviado && !fotos) return;
    form.prepend(caja);
  }

  leer(clave).then(function (datos) {
    if (datos && Date.now() - datos.guardado < VIGENCIA) {
      restaurar(datos);
      aviso(datos);
    } else if (datos) {
      return borrar(clave);
    }
  }).catch(function () { /* sin borrador */ }).finally(function () {
    listo = true;
    if (reenviado) guardar();
  });

  form.addEventListener('input', programar);
  form.addEventListener('change', programar);
  // La firma no dispara «input»: se guarda al levantar el dedo
  form.addEventListener('pointerup', function (e) { if (e.target.closest('.firma-pad')) setTimeout(programar, 50); });
  form.addEventListener('click', function (e) { if (e.target.closest('.borrar-firma, .agregar-fila')) setTimeout(programar, 50); });
  // Llamada, notificación o cambio de aplicación: se guarda ya
  document.addEventListener('visibilitychange', function () { if (document.visibilityState === 'hidden') guardar(); });
  window.addEventListener('pagehide', guardar);
  // Al confirmar el envío: se marca para borrar el borrador cuando el servidor diga que el acta quedó registrada
  form.addEventListener('acta:enviando', function () {
    guardar();
    try { sessionStorage.setItem(ENVIADA, clave); } catch (e) { /* nada */ }
  });
})();
