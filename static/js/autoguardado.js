/* Autoguardado de formularios (23/11/2026): lo mismo que el borrador del acta, en todos los formularios de la plataforma.
   Mientras alguien diligencia una solicitud, un informe, una propuesta o cualquier formulario con texto, lo que escribe,
   marca y adjunta se guarda solo en este dispositivo (IndexedDB del navegador). Si se cierra la pestaña, se va el
   internet, entra una llamada o la sesión vence, al volver a abrir la misma página se recupera tal cual.

   - El borrador es por usuario, por página y por formulario. Se guarda al escribir (cada segundo como máximo) y de
     inmediato cuando la página pasa a segundo plano.
   - Al enviar con éxito el borrador se borra; si el servidor devuelve el formulario con errores, se conserva.
   - Si el servidor devolvió errores, no se pisa lo que muestra: el borrador queda guardado para el siguiente intento.
   - Un borrador de más de 7 días se descarta. Siempre se puede «Descartar borrador».
   - Aplica a formularios POST con al menos un texto largo o tres campos; no aplica al acta (tiene el suyo), a los
     formularios con contraseña, a las ventanas emergentes ni a los marcados con data-autoguardado="no".
     Para filas agregadas con un botón, el botón lleva data-agregar-fila="prefijo" (o la clase .agregar-fila). */
(function () {
  const BD = 'janotec-borradores', ALMACEN = 'actas', ENVIADO = 'autoguardado-enviado', VIGENCIA = 7 * 24 * 3600 * 1000;
  const usuario = document.body.dataset.usuario;
  if (!window.indexedDB || !usuario) return;

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

  const OMITIR = /^(csrfmiddlewaretoken|accion|latitud|longitud)$|-(TOTAL|INITIAL|MIN_NUM|MAX_NUM)_FORMS$/;
  const ruta = location.pathname;
  function hayErrores(raiz) {
    return !!raiz.querySelector('.is-invalid, .errorlist, div.text-danger.small, .invalid-feedback:not(:empty)');
  }

  // ¿Se envió un formulario en la página anterior? Si esta página no trae errores, quedó guardado: el borrador sobra.
  let enviado = null;
  try { enviado = JSON.parse(sessionStorage.getItem(ENVIADO) || 'null'); sessionStorage.removeItem(ENVIADO); } catch (e) { /* nada */ }
  let yaGuardado = '';  // el formulario que se acaba de guardar con éxito: su borrador no se recupera
  if (enviado && !(enviado.ruta === ruta && (hayErrores(document) || document.querySelector('.alert-danger')))) {
    yaGuardado = enviado.clave;
    borrar(enviado.clave).catch(function () {});
  }

  function editables(form) {
    return [...form.elements].filter(function (c) {
      return c.name && !OMITIR.test(c.name) && !['submit', 'button', 'reset', 'hidden', 'password'].includes(c.type) && !c.readOnly;
    });
  }
  function aplica(form) {
    if ((form.getAttribute('method') || '').toLowerCase() !== 'post') return false;
    if (form.dataset.borrador !== undefined || form.dataset.autoguardado === 'no' || form.closest('.modal')) return false;
    if (form.querySelector('input[type=password]')) return false;
    if (form.dataset.autoguardado === 'si') return true;
    const campos = editables(form);
    return campos.some(function (c) { return c.tagName === 'TEXTAREA'; }) || campos.length >= 3;
  }

  const formularios = [...document.querySelectorAll('form')].filter(aplica);
  formularios.forEach(function (form, i) {
    const version = form.dataset.versionBorrador;
    const clave = 'auto:' + usuario + ':' + ruta + location.search + '#' + (form.id || i) + (version ? ':' + version : '');
    const conErrores = hayErrores(form);
    let listo = false, pendiente = null;
    let editado = false;

    function tomar() {
      const datos = { guardado: Date.now(), valores: {}, archivos: {}, filas: {} };
      editables(form).forEach(function (c) {
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
      form.querySelectorAll('input[name$="-TOTAL_FORMS"]').forEach(function (t) {
        datos.filas[t.name.replace(/-TOTAL_FORMS$/, '')] = parseInt(t.value, 10) || 0;
      });
      return datos;
    }
    function vacio(datos) {
      return !Object.values(datos.valores).some(function (v) { return v.some(function (x) { return String(x).trim(); }); })
        && !Object.keys(datos.archivos).length;
    }
    let inicial = null;  // lo que trae la página al abrirla: si no cambia nada, no se guarda borrador
    function guardar() {
      clearTimeout(pendiente); pendiente = null;
      if (!listo || (version && !editado)) return;
      const datos = tomar();
      if (JSON.stringify(datos.valores) === inicial && !Object.keys(datos.archivos).length) {
        if (version) {
          try { localStorage.removeItem(clave); } catch (e) { /* almacenamiento bloqueado */ }
          borrar(clave).catch(function () {});
        }
        return;
      }
      if (vacio(datos)) { borrar(clave).catch(function () {}); return; }
      if (version) {
        // Escribe antes de que F5 descargue la pagina, incluso entre pulsaciones.
        try { localStorage.setItem(clave, JSON.stringify(datos)); return; } catch (e) { /* queda IndexedDB */ }
      }
      escribir(clave, datos).catch(function () { /* memoria del navegador llena o bloqueada */ });
    }
    function programar() {
      if (version && listo) {
        editado = true;
        guardar();
      } else if (!pendiente) {
        pendiente = setTimeout(guardar, 1000);
      }
    }

    function ponerArchivos(c, lista) {
      if (!lista || !lista.length || !window.DataTransfer) return;
      try {
        const dt = new DataTransfer();
        lista.forEach(function (f) { dt.items.add(f); });
        c.files = dt.files;
      } catch (e) { /* navegador antiguo: los archivos se vuelven a adjuntar */ }
    }
    function restaurar(datos) {
      // Primero las filas agregadas, para que existan sus campos
      Object.keys(datos.filas || {}).forEach(function (prefijo) {
        const total = form.querySelector('[name="' + prefijo + '-TOTAL_FORMS"]');
        const boton = form.querySelector('[data-agregar-fila="' + prefijo + '"], .agregar-fila[data-prefijo="' + prefijo + '"]')
          || document.querySelector('[data-agregar-fila="' + prefijo + '"]');
        let vueltas = 0;
        const limite = version ? 1000 : 60;
        while (total && boton && parseInt(total.value, 10) < datos.filas[prefijo] && vueltas++ < limite) {
          const antes = total.value;
          boton.click();
          if (total.value === antes) break;
        }
      });
      editables(form).forEach(function (c) {
        const v = datos.valores[c.name];
        if (c.type === 'file') { ponerArchivos(c, (datos.archivos || {})[c.name]); return; }
        if (v === undefined) return;
        if (c.type === 'checkbox' || c.type === 'radio') c.checked = v.indexOf(c.value) !== -1;
        else if (c.multiple && c.options) [...c.options].forEach(function (o) { o.selected = v.indexOf(o.value) !== -1; });
        else c.value = v[0];
      });
      form.dispatchEvent(new Event('borrador-restaurado'));
      // Que la página recalcule lo que depende de los campos (totales, secciones que se muestran, contadores)
      editables(form).forEach(function (c) {
        c.dispatchEvent(new Event(c.type === 'checkbox' || c.type === 'radio' || c.tagName === 'SELECT' ? 'change' : 'input', { bubbles: true }));
      });
    }
    function aviso(datos) {
      const caja = document.createElement('div');
      caja.className = 'alert alert-info d-flex flex-wrap align-items-center gap-2 small aviso-autoguardado';
      caja.setAttribute('role', 'status');
      caja.innerHTML = '<i class="bi bi-arrow-counterclockwise"></i><span class="me-auto"></span>'
        + '<button type="button" class="btn btn-sm btn-outline-secondary">Descartar borrador</button>';
      const cuando = new Date(datos.guardado).toLocaleString('es-CO', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' });
      caja.querySelector('span').textContent = 'Se recuperó lo que había escrito el ' + cuando + ' y no alcanzó a guardar. Revíselo y continúe.';
      caja.querySelector('button').addEventListener('click', function () {
        if (!confirm('¿Descartar lo recuperado y volver a como estaba?')) return;
        listo = false;
        if (version) try { localStorage.removeItem(clave); } catch (e) { /* almacenamiento bloqueado */ }
        borrar(clave).finally(function () { location.replace(location.pathname + location.search); });
      });
      form.prepend(caja);
    }

    inicial = JSON.stringify(tomar().valores);
    let local = null;
    if (version) try { local = JSON.parse(localStorage.getItem(clave)); } catch (e) { /* almacenamiento bloqueado */ }
    (local ? Promise.resolve(local) : leer(clave)).then(function (datos) {
      if (!datos || clave === yaGuardado) return;
      if (Date.now() - datos.guardado > VIGENCIA) {
        if (version) try { localStorage.removeItem(clave); } catch (e) { /* almacenamiento bloqueado */ }
        return borrar(clave);
      }
      if (conErrores) return;  // el servidor muestra lo enviado con sus errores: se respeta
      restaurar(datos);
      aviso(datos);
    }).catch(function () { /* sin borrador */ }).finally(function () { listo = true; });

    form.addEventListener('input', programar);
    form.addEventListener('change', programar);
    form.addEventListener('click', function (e) { if (e.target.closest('.agregar-fila, [data-agregar-fila], .quitar-fila')) setTimeout(programar, 50); });
    document.addEventListener('visibilitychange', function () { if (document.visibilityState === 'hidden') guardar(); });
    window.addEventListener('pagehide', guardar);
    form.addEventListener('submit', function () {
      guardar();
      // Los formularios versionados cambian de clave cuando el servidor guarda.
      if (version) return;
      try { sessionStorage.setItem(ENVIADO, JSON.stringify({ clave: clave, ruta: ruta })); } catch (e) { /* nada */ }
    });
  });
})();
