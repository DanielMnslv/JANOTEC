/* Ayuda de redacción para los campos que escribe el técnico en el acta (tickets/redaccion.py).
 *
 * A cada <textarea data-redaccion="tipo"> le agrega:
 *  - una guía corta de qué escribir,
 *  - frases rápidas para tocar (útil en el celular),
 *  - corrección automática al salir del campo: espacios de sobra, mayúscula inicial y punto final,
 *  - un aviso si el texto quedó muy corto,
 *  - el corrector en español del navegador o del teclado del celular (subraya lo mal escrito),
 *  - el botón «Revisar ortografía» (LanguageTool, gratuito): lista los errores y cada sugerencia se aplica con
 *    un toque, o todas con «Corregir todo»,
 *  - el botón «Mejorar redacción» (Claude, con costo), solo si el servidor lo tiene activado. El técnico ve la propuesta
 *    y decide si la usa; nada se cambia sin que él lo acepte.
 * La configuración llega en <script id="redaccion-config" type="application/json">.
 */
(function () {
  const nodo = document.getElementById('redaccion-config');
  if (!nodo) return;
  const config = JSON.parse(nodo.textContent);

  function el(tag, clase, texto) {
    const e = document.createElement(tag);
    if (clase) e.className = clase;
    if (texto) e.textContent = texto;
    return e;
  }

  // Quita espacios de sobra, pone mayúscula al inicio de cada línea y punto al final del texto
  function pulir(texto) {
    const lineas = texto.split('\n').map(function (l) {
      l = l.replace(/[ \t]+/g, ' ').trim();
      return l ? l.charAt(0).toLocaleUpperCase('es') + l.slice(1) : l;
    });
    let r = lineas.join('\n').replace(/\n{3,}/g, '\n\n').trim();
    if (/[0-9A-Za-zÁÉÍÓÚÜÑáéíóúüñ)]$/.test(r)) r += '.';
    return r;
  }

  function palabras(texto) { return texto.trim() ? texto.trim().split(/\s+/).length : 0; }

  function insertar(campo, frase) {
    const v = campo.value, ini = campo.selectionStart ?? v.length, fin = campo.selectionEnd ?? v.length;
    const antes = v.slice(0, ini), sep = antes && !/\s$/.test(antes) ? ' ' : '';
    campo.value = antes + sep + frase + v.slice(fin);
    const pos = (antes + sep + frase).length;
    campo.focus();
    campo.setSelectionRange(pos, pos);
    campo.dispatchEvent(new Event('input', { bubbles: true }));
  }

  function token(campo) {
    const f = campo.form && campo.form.querySelector('[name=csrfmiddlewaretoken]');
    return f ? f.value : '';
  }

  function preparar(campo) {
    if (campo.dataset.redaccionLista) return;
    const tipo = config.tipos[campo.dataset.redaccion];
    if (!tipo) return;
    campo.dataset.redaccionLista = '1';

    const caja = el('div', 'redaccion-ayuda mt-1');
    caja.append(el('div', 'form-text mt-0 mb-1', '✍️ ' + tipo.guia));

    const fila = el('div', 'd-flex flex-wrap gap-1 align-items-center');
    tipo.frases.forEach(function (frase) {
      const b = el('button', 'btn btn-sm btn-outline-secondary rounded-pill py-0 px-2', frase.trim());
      b.type = 'button';
      b.addEventListener('click', function () { insertar(campo, frase); });
      fila.append(b);
    });

    const aviso = el('div', 'small text-warning-emphasis mt-1');
    aviso.hidden = true;
    const propuesta = el('div', 'border rounded p-2 mt-2 bg-body-tertiary');
    propuesta.hidden = true;

    // Corrector del navegador / teclado del celular, en español
    campo.spellcheck = true;
    campo.lang = 'es';
    campo.setAttribute('autocorrect', 'on');
    campo.setAttribute('autocapitalize', 'sentences');

    if (config.ortografia) {
      const revisarBtn = el('button', 'btn btn-sm btn-outline-success rounded-pill py-0 px-2 ms-auto');
      revisarBtn.type = 'button';
      revisarBtn.innerHTML = '<i class="bi bi-spellcheck"></i> Revisar ortografía';
      revisarBtn.addEventListener('click', function () {
        if (!campo.value.trim()) { aviso.textContent = 'Escriba primero el texto; luego lo revisamos.'; aviso.hidden = false; return; }
        const limpio = pulir(campo.value);
        if (limpio !== campo.value) campo.value = limpio;
        revisarBtn.disabled = true;
        const original = revisarBtn.innerHTML;
        revisarBtn.innerHTML = '<span class="spinner-border spinner-border-sm"></span> Revisando…';
        const texto = campo.value, datos = new FormData();
        datos.append('texto', texto);
        fetch(config.ortografia, { method: 'POST', body: datos, headers: { 'X-CSRFToken': token(campo) }, credentials: 'same-origin' })
          .then(function (r) { return r.json().catch(function () { return { error: 'No se pudo revisar. Intente de nuevo.' }; }); })
          .then(function (r) { mostrarErrores(r, texto); })
          .catch(function () { mostrarErrores({ error: 'Sin conexión. El texto se guarda tal como lo escribió.' }); })
          .finally(function () { revisarBtn.disabled = false; revisarBtn.innerHTML = original; });
      });
      fila.append(revisarBtn);
    }

    // Lista de errores de LanguageTool: cada uno con sus sugerencias; se aplican sobre el texto revisado
    function mostrarErrores(r, revisado) {
      propuesta.replaceChildren();
      propuesta.hidden = false;
      if (r.error) { propuesta.append(el('div', 'small text-danger', r.error)); return; }
      let pendientes = r.errores.map(function (e) { return Object.assign({}, e); });
      function cerrarSiListo() {
        if (pendientes.some(function (e) { return e.sugerencias.length; })) return;
        propuesta.replaceChildren(el('div', 'small text-success', '✔ Texto revisado: sin errores pendientes.'));
      }
      function aplicar(err, valor) {
        if (campo.value !== revisado) {  // lo editaron después de revisar: las posiciones ya no sirven
          propuesta.replaceChildren(el('div', 'small text-warning-emphasis', 'El texto cambió. Toque «Revisar ortografía» otra vez.'));
          return;
        }
        revisado = revisado.slice(0, err.inicio) + valor + revisado.slice(err.inicio + err.largo);
        const delta = valor.length - err.largo;
        pendientes = pendientes.filter(function (e) { return e !== err; });
        pendientes.forEach(function (e) { if (e.inicio > err.inicio) e.inicio += delta; });
        campo.value = revisado;
        campo.dispatchEvent(new Event('input', { bubbles: true }));
        dibujar();
      }
      function dibujar() {
        propuesta.replaceChildren();
        const conSugerencia = pendientes.filter(function (e) { return e.sugerencias.length; });
        if (!pendientes.length) { cerrarSiListo(); return; }
        const cabeza = el('div', 'd-flex align-items-center mb-1');
        cabeza.append(el('div', 'small fw-medium me-auto', pendientes.length + (pendientes.length === 1 ? ' posible error' : ' posibles errores')));
        if (conSugerencia.length > 1) {
          const todo = el('button', 'btn btn-sm btn-success py-0', 'Corregir todo');
          todo.type = 'button';
          todo.addEventListener('click', function () {
            // de atrás hacia adelante, para no mover las posiciones de los que faltan
            conSugerencia.slice().sort(function (a, b) { return b.inicio - a.inicio; })
              .forEach(function (e) { if (campo.value === revisado) aplicar(e, e.sugerencias[0]); });
          });
          cabeza.append(todo);
        }
        const cerrar = el('button', 'btn btn-sm btn-light py-0 ms-1', 'Cerrar');
        cerrar.type = 'button';
        cerrar.addEventListener('click', function () { propuesta.hidden = true; });
        cabeza.append(cerrar);
        propuesta.append(cabeza);
        pendientes.forEach(function (e) {
          const item = el('div', 'small border-top pt-1 mt-1');
          const mal = el('span', 'text-danger text-decoration-line-through fw-medium', revisado.substr(e.inicio, e.largo) || '·');
          item.append(mal, document.createTextNode(' — ' + e.mensaje + ' '));
          e.sugerencias.forEach(function (s) {
            const b = el('button', 'btn btn-sm btn-outline-success py-0 px-2 me-1 mt-1', s);
            b.type = 'button';
            b.addEventListener('click', function () { aplicar(e, s); });
            item.append(b);
          });
          const ignorar = el('button', 'btn btn-sm btn-link py-0 px-1 mt-1', 'Ignorar');
          ignorar.type = 'button';
          ignorar.addEventListener('click', function () { pendientes = pendientes.filter(function (x) { return x !== e; }); dibujar(); });
          item.append(ignorar);
          propuesta.append(item);
        });
      }
      if (!pendientes.length) {
        propuesta.append(el('div', 'small text-success', '✔ No se encontraron errores de ortografía.'));
        return;
      }
      dibujar();
    }

    if (config.url) {
      const mejorar = el('button', 'btn btn-sm btn-outline-primary rounded-pill py-0 px-2 ' + (config.ortografia ? 'ms-1' : 'ms-auto'));
      mejorar.type = 'button';
      mejorar.innerHTML = '<i class="bi bi-stars"></i> Mejorar redacción';
      mejorar.addEventListener('click', function () {
        if (!campo.value.trim()) { aviso.textContent = 'Escriba primero el texto; luego lo mejoramos.'; aviso.hidden = false; return; }
        mejorar.disabled = true;
        const original = mejorar.innerHTML;
        mejorar.innerHTML = '<span class="spinner-border spinner-border-sm"></span> Mejorando…';
        const datos = new FormData();
        datos.append('texto', campo.value);
        datos.append('tipo', campo.dataset.redaccion);
        fetch(config.url, { method: 'POST', body: datos, headers: { 'X-CSRFToken': token(campo) }, credentials: 'same-origin' })
          .then(function (r) { return r.json().catch(function () { return { error: 'No se pudo mejorar. Intente de nuevo.' }; }); })
          .then(function (r) { mostrar(r); })
          .catch(function () { mostrar({ error: 'Sin conexión. El texto se guarda tal como lo escribió.' }); })
          .finally(function () { mejorar.disabled = false; mejorar.innerHTML = original; });
      });
      fila.append(mejorar);
    }

    function mostrar(r) {
      propuesta.replaceChildren();
      if (r.error) {
        propuesta.append(el('div', 'small text-danger', r.error));
      } else {
        propuesta.append(el('div', 'small fw-medium mb-1', 'Propuesta de redacción:'));
        const texto = el('div', 'small mb-2', r.texto);
        texto.style.whiteSpace = 'pre-line';
        const usar = el('button', 'btn btn-sm btn-primary me-1', 'Usar esta');
        usar.type = 'button';
        usar.addEventListener('click', function () {
          campo.value = r.texto;
          campo.dispatchEvent(new Event('input', { bubbles: true }));
          propuesta.hidden = true;
          revisar();
        });
        const dejar = el('button', 'btn btn-sm btn-light', 'Dejar la mía');
        dejar.type = 'button';
        dejar.addEventListener('click', function () { propuesta.hidden = true; });
        propuesta.append(texto, usar, dejar);
      }
      propuesta.hidden = false;
    }

    function revisar() {
      const n = palabras(campo.value);
      aviso.hidden = !(n && n < tipo.minimo);
      aviso.textContent = 'Muy corto para el acta: cuente un poco más (' + tipo.guia.toLowerCase() + ')';
    }

    campo.addEventListener('blur', function () {
      const limpio = pulir(campo.value);
      if (limpio !== campo.value) campo.value = limpio;
      revisar();
    });

    caja.append(fila, aviso, propuesta);
    campo.after(caja);
  }

  document.querySelectorAll('textarea[data-redaccion]').forEach(preparar);
  // Filas que se agregan después (equipos, materiales): se preparan al tocarlas
  document.addEventListener('focusin', function (ev) {
    if (ev.target.matches && ev.target.matches('textarea[data-redaccion]')) preparar(ev.target);
  });
})();
