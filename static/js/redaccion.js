/* Ayuda de redacción para los campos que escribe el técnico en el acta (tickets/redaccion.py).
 *
 * A cada <textarea data-redaccion="tipo"> le agrega:
 *  - una guía corta de qué escribir,
 *  - frases rápidas para tocar (útil en el celular),
 *  - corrección automática al salir del campo: espacios de sobra, mayúscula inicial y punto final,
 *  - un aviso si el texto quedó muy corto,
 *  - el botón «Mejorar redacción» (Claude), solo si el servidor lo tiene activado. El técnico ve la propuesta
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

    if (config.url) {
      const mejorar = el('button', 'btn btn-sm btn-outline-primary rounded-pill py-0 px-2 ms-auto');
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
