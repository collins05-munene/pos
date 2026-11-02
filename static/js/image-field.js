/*
 * Photo fields for the product form: file picker + camera.
 *
 * Markup (see products/_image_field.html): a [data-img-field] wrapper holding the real, named
 * <input type=file data-img-input> plus buttons marked data-img-pick / data-img-camera / data-img-clear
 * and an unnamed <input data-img-capture capture=environment>.
 *
 * "Take photo":
 *   - phones / tablets (coarse pointer): open the native camera app through the capture input --
 *     best autofocus, flash and quality, and it works on plain http as well;
 *   - desktops / laptops: a live in-page camera (getUserMedia, needs https or localhost), and if that
 *     is unavailable or refused we fall back to the file picker.
 * Either way the picture is copied into the named input, so the server receives an ordinary upload
 * and compresses it exactly like a picked file.
 *
 * Everything is event-delegated, so rows cloned by "+ Add Another Variant" work with no extra wiring.
 */
(function () {
  'use strict';

  var MAX_SIDE = 1200, SKIP_BELOW = 1024 * 1024;
  var canAssign = !!(window.DataTransfer && typeof DataTransfer === 'function');
  try { if (canAssign) new DataTransfer(); } catch (e) { canAssign = false; }
  var hasLiveCamera = !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia && window.isSecureContext);
  var isTouch = !!(window.matchMedia && window.matchMedia('(pointer: coarse)').matches);

  function parts(el) {
    var field = el.closest('[data-img-field]');
    if (!field) return null;
    return {
      field: field,
      input: field.querySelector('[data-img-input]'),
      capture: field.querySelector('[data-img-capture]'),
      preview: field.querySelector('[data-img-preview]'),
      clear: field.querySelector('[data-img-clear]'),
      remove: field.querySelector('input[type=checkbox][name$="remove_image"]')
    };
  }

  // Phone photos are several MB. Shrink them here so only ~100-300 KB travels over mobile data.
  // Any failure falls back to the original; the server compresses either way.
  async function shrink(file) {
    if (file.size < SKIP_BELOW || !window.createImageBitmap) return file;
    try {
      var bmp = await createImageBitmap(file);
      var k = Math.min(1, MAX_SIDE / Math.max(bmp.width, bmp.height));
      var c = document.createElement('canvas');
      c.width = Math.round(bmp.width * k); c.height = Math.round(bmp.height * k);
      var ctx = c.getContext('2d');
      ctx.fillStyle = '#fff'; ctx.fillRect(0, 0, c.width, c.height);
      ctx.drawImage(bmp, 0, 0, c.width, c.height);
      var blob = await new Promise(function (r) { c.toBlob(r, 'image/jpeg', 0.85); });
      if (!blob || blob.size >= file.size) return file;
      return new File([blob], file.name.replace(/\.\w+$/, '') + '.jpg', { type: 'image/jpeg' });
    } catch (e) { return file; }
  }

  function showPreview(p, file) {
    if (p.preview._objectUrl) URL.revokeObjectURL(p.preview._objectUrl);
    var img = document.createElement('img');
    img.alt = '';
    p.preview._objectUrl = URL.createObjectURL(file);
    img.src = p.preview._objectUrl;
    p.preview.innerHTML = '';
    p.preview.appendChild(img);
    if (p.clear) p.clear.hidden = false;
    if (p.remove) p.remove.checked = false;   // a new photo replaces the old one; don't also delete it
  }

  function restorePreview(p) {
    if (p.preview._objectUrl) { URL.revokeObjectURL(p.preview._objectUrl); p.preview._objectUrl = null; }
    var url = p.preview.getAttribute('data-original-url');
    p.preview.innerHTML = '';
    if (url) {
      var img = document.createElement('img'); img.alt = ''; img.src = url; p.preview.appendChild(img);
    } else {
      var span = document.createElement('span'); span.textContent = 'No image'; p.preview.appendChild(span);
    }
    if (p.clear) p.clear.hidden = true;
  }

  // Put `file` into the real input (the one that is submitted) and show it.
  async function useFile(p, file) {
    var small = await shrink(file);
    var dt = new DataTransfer();
    dt.items.add(small);
    p.input.files = dt.files;
    showPreview(p, small);
  }

  /* ---- live camera (desktop) ------------------------------------------------------------ */
  var modal = null;

  function buildModal() {
    var m = document.createElement('div');
    m.className = 'cam-modal';
    m.hidden = true;
    m.innerHTML =
      '<div class="cam-box" role="dialog" aria-modal="true" aria-label="Take a photo">' +
        '<video class="cam-video" playsinline muted autoplay></video>' +
        '<div class="cam-msg" role="status"></div>' +
        '<div class="cam-bar">' +
          '<button type="button" class="img-btn" data-cam="switch" hidden>Switch camera</button>' +
          '<button type="button" class="img-btn img-btn--primary" data-cam="snap">Capture</button>' +
          '<button type="button" class="img-btn" data-cam="close">Cancel</button>' +
        '</div>' +
      '</div>';
    document.body.appendChild(m);
    return m;
  }

  var stream = null, target = null, facing = 'environment';

  function stopStream() {
    if (stream) { stream.getTracks().forEach(function (t) { t.stop(); }); stream = null; }
    if (modal) modal.querySelector('video').srcObject = null;
  }

  function closeModal() {
    stopStream();
    if (modal) modal.hidden = true;
    document.removeEventListener('keydown', onKey);
    target = null;
  }

  function onKey(e) { if (e.key === 'Escape') closeModal(); }

  async function startStream() {
    var msg = modal.querySelector('.cam-msg');
    msg.textContent = '';
    stopStream();
    stream = await navigator.mediaDevices.getUserMedia({
      video: { facingMode: { ideal: facing }, width: { ideal: 1920 }, height: { ideal: 1080 } },
      audio: false
    });
    var video = modal.querySelector('video');
    video.srcObject = stream;
    try { await video.play(); } catch (e) { /* autoplay is already requested via the attribute */ }
    var devices = await navigator.mediaDevices.enumerateDevices();
    var cams = devices.filter(function (d) { return d.kind === 'videoinput'; });
    modal.querySelector('[data-cam=switch]').hidden = cams.length < 2;
  }

  async function openLiveCamera(p) {
    if (!modal) modal = buildModal();
    target = p;
    modal.hidden = false;
    document.addEventListener('keydown', onKey);
    try {
      await startStream();
      modal.querySelector('[data-cam=snap]').focus();
    } catch (err) {
      // Permission refused, no camera, or in use by another app: use the file picker instead.
      closeModal();
      var why = (err && err.name === 'NotAllowedError')
        ? 'Camera access was blocked, so choose a photo from your files instead. You can allow the camera in the browser\'s site settings.'
        : 'No camera could be opened, so choose a photo from your files instead.';
      alert(why);
      if (p.capture) p.capture.click(); else p.input.click();
    }
  }

  async function snap() {
    var video = modal.querySelector('video');
    if (!target || !video.videoWidth) return;
    var p = target;
    var k = Math.min(1, MAX_SIDE / Math.max(video.videoWidth, video.videoHeight));
    var c = document.createElement('canvas');
    c.width = Math.round(video.videoWidth * k); c.height = Math.round(video.videoHeight * k);
    c.getContext('2d').drawImage(video, 0, 0, c.width, c.height);
    var blob = await new Promise(function (r) { c.toBlob(r, 'image/jpeg', 0.88); });
    closeModal();
    if (!blob) return;
    await useFile(p, new File([blob], 'camera-' + Date.now() + '.jpg', { type: 'image/jpeg' }));
  }

  document.addEventListener('click', function (e) {
    var cam = e.target.closest('[data-cam]');
    if (cam && modal && modal.contains(cam)) {
      var what = cam.getAttribute('data-cam');
      if (what === 'close') closeModal();
      else if (what === 'snap') snap();
      else if (what === 'switch') {
        facing = facing === 'environment' ? 'user' : 'environment';
        startStream().catch(function () { facing = facing === 'environment' ? 'user' : 'environment'; });
      }
      return;
    }
    if (modal && e.target === modal) { closeModal(); return; }   // click on the dark backdrop

    var btn = e.target.closest('[data-img-pick], [data-img-camera], [data-img-clear]');
    if (!btn) return;
    var p = parts(btn);
    if (!p || !p.input) return;

    if (btn.hasAttribute('data-img-pick')) {
      p.input.click();
    } else if (btn.hasAttribute('data-img-camera')) {
      if (isTouch || !hasLiveCamera) {
        if (p.capture) p.capture.click(); else p.input.click();
      } else {
        openLiveCamera(p);
      }
    } else if (btn.hasAttribute('data-img-clear')) {
      p.input.value = '';
      restorePreview(p);
    }
  });

  document.addEventListener('change', async function (e) {
    var el = e.target;
    var isMain = el.matches && el.matches('[data-img-input]');
    var isCapture = el.matches && el.matches('[data-img-capture]');
    if (!isMain && !isCapture) return;
    var p = parts(el);
    var f = el.files && el.files[0];
    if (!p || !f) return;
    if (isCapture) {
      await useFile(p, f);
      el.value = '';              // allow taking another photo straight after
    } else {
      showPreview(p, f);
      var small = await shrink(f);
      if (small !== f) {
        var dt = new DataTransfer(); dt.items.add(small); p.input.files = dt.files;
      }
    }
  });

  // Reveal the camera buttons only where we can actually copy a photo into the form.
  function reveal(root) {
    if (!canAssign) return;
    (root || document).querySelectorAll('[data-img-camera]').forEach(function (b) { b.hidden = false; });
  }
  reveal(document);
  // Rows added later (+ Add Another Variant) arrive with the button still hidden.
  new MutationObserver(function (muts) {
    muts.forEach(function (m) {
      m.addedNodes.forEach(function (n) { if (n.nodeType === 1) reveal(n); });
    });
  }).observe(document.documentElement, { childList: true, subtree: true });
})();
