/* Firmas a mano (con el dedo o el mouse) en cualquier formulario: un .firma-pad con su <canvas> y el campo oculto
   (data-campo = id del campo) donde queda la imagen PNG. Lo usan el acta de soporte, el acta de entrega y la
   aprobación de salida de bodega. Si el formulario vuelve con errores, se conserva la firma ya hecha. */
(function () {
  function activar(caja) {
    if (caja.dataset.activo) return;
    caja.dataset.activo = '1';
    const lienzo = caja.querySelector('canvas');
    const campo = document.getElementById(caja.dataset.campo);
    const ctx = lienzo.getContext('2d');
    let dibujando = false, trazos = false;
    function ajustar() {
      const r = window.devicePixelRatio || 1, ancho = lienzo.clientWidth, alto = lienzo.clientHeight;
      if (!ancho) return;
      lienzo.width = ancho * r; lienzo.height = alto * r;
      ctx.setTransform(r, 0, 0, r, 0, 0);
      ctx.lineWidth = 2.2; ctx.lineCap = 'round'; ctx.lineJoin = 'round'; ctx.strokeStyle = '#1c1c1a';
      if (campo.value) { const img = new Image(); img.onload = function () { ctx.drawImage(img, 0, 0, lienzo.clientWidth, lienzo.clientHeight); }; img.src = campo.value; trazos = true; caja.classList.add('con-firma'); }
    }
    function punto(e) { const b = lienzo.getBoundingClientRect(); return [e.clientX - b.left, e.clientY - b.top]; }
    lienzo.addEventListener('pointerdown', function (e) { if (!lienzo.width) ajustar(); dibujando = true; trazos = true; lienzo.setPointerCapture(e.pointerId); const [x, y] = punto(e); ctx.beginPath(); ctx.moveTo(x, y); caja.classList.add('con-firma'); });
    lienzo.addEventListener('pointermove', function (e) { if (!dibujando) return; const [x, y] = punto(e); ctx.lineTo(x, y); ctx.stroke(); });
    ['pointerup', 'pointercancel'].forEach(function (ev) { lienzo.addEventListener(ev, function () { dibujando = false; if (trazos) campo.value = lienzo.toDataURL('image/png'); }); });
    caja.querySelector('.borrar-firma').addEventListener('click', function () { ctx.clearRect(0, 0, lienzo.width, lienzo.height); trazos = false; campo.value = ''; caja.classList.remove('con-firma'); });
    caja.addEventListener('firma:cargar', ajustar);  // el borrador del acta repone una firma ya hecha
    ajustar();
  }
  document.querySelectorAll('.firma-pad').forEach(activar);
  // Los recuadros dentro de un <details> cerrado no tienen tamaño: se preparan al abrirlo
  document.addEventListener('toggle', function (e) { if (e.target.open) e.target.querySelectorAll('.firma-pad').forEach(function (c) { delete c.dataset.activo; activar(c); }); }, true);
})();
