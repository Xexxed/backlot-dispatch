/* Voice-note capture: microphone -> 16 kHz 16-bit mono WAV -> multipart POST.
 *
 * Web Audio API (getUserMedia + AudioContext + ScriptProcessorNode) with a
 * hand-rolled WAV encoder, so capture behaves identically across browsers —
 * MediaRecorder is intentionally not used (desktop Chrome only emits
 * audio/webm, which the intake agent cannot accept).
 *
 * Submission is a plain form navigation (no fetch), so 303 redirects and the
 * graceful-fallback pages just work. Where capture is impossible (insecure
 * context, no mic, permission denied) the widget degrades to a hint and the
 * text box remains the path.
 */
(function () {
  "use strict";

  var SAMPLE_RATE = 16000;
  var MAX_SECONDS = 120; // client cap ~2 min: auto-stop + submit
  var CHUNK = 4096; // ScriptProcessorNode buffer size

  function encodeWav(samples) {
    var buffer = new ArrayBuffer(44 + samples.length * 2);
    var view = new DataView(buffer);
    function writeString(offset, s) {
      for (var i = 0; i < s.length; i++) view.setUint8(offset + i, s.charCodeAt(i));
    }
    writeString(0, "RIFF");
    view.setUint32(4, 36 + samples.length * 2, true);
    writeString(8, "WAVE");
    writeString(12, "fmt ");
    view.setUint32(16, 16, true); // PCM fmt chunk size
    view.setUint16(20, 1, true); // PCM
    view.setUint16(22, 1, true); // mono
    view.setUint32(24, SAMPLE_RATE, true);
    view.setUint32(28, SAMPLE_RATE * 2, true); // byte rate
    view.setUint16(32, 2, true); // block align
    view.setUint16(34, 16, true); // bits per sample
    writeString(36, "data");
    view.setUint32(40, samples.length * 2, true);
    var offset = 44;
    for (var i = 0; i < samples.length; i++, offset += 2) {
      var s = Math.max(-1, Math.min(1, samples[i]));
      view.setInt16(offset, s < 0 ? s * 0x8000 : s * 0x7fff, true);
    }
    return buffer;
  }

  function fmtClock(seconds) {
    var m = Math.floor(seconds / 60);
    var s = seconds % 60;
    return m + ":" + (s < 10 ? "0" : "") + s;
  }

  // The widget sits inside the incident form, so state the AD set there
  // (completed-scene checkboxes, demo clock) must travel with the voice
  // submission — otherwise the pipeline would replan as if nothing was shot.
  function copyStateFields(form, hostForm) {
    if (!hostForm) return;
    var checked = hostForm.querySelectorAll('input[name="completed"]:checked');
    for (var i = 0; i < checked.length; i++) {
      var c = document.createElement("input");
      c.type = "hidden";
      c.name = "completed";
      c.value = checked[i].value;
      form.appendChild(c);
    }
    var clock = hostForm.querySelector('input[name="now_override"]');
    if (clock && clock.value) {
      var t = document.createElement("input");
      t.type = "hidden";
      t.name = "now_override";
      t.value = clock.value;
      form.appendChild(t);
    }
  }

  function submitWav(blob, hostForm) {
    var form = document.createElement("form");
    form.method = "post";
    form.action = "/incident/voice";
    form.enctype = "multipart/form-data";
    form.hidden = true;
    var input = document.createElement("input");
    input.type = "file";
    input.name = "audio";
    var bundle = new DataTransfer();
    bundle.items.add(new File([blob], "voice-note.wav", { type: "audio/wav" }));
    input.files = bundle.files;
    form.appendChild(input);
    copyStateFields(form, hostForm);
    document.body.appendChild(form);
    form.submit(); // plain navigation: lands on the sandbox/review/fallback page
  }

  function init(widget) {
    var button = widget.querySelector("[data-voice-button]");
    var status = widget.querySelector("[data-voice-status]");
    if (!button || !status) return;

    if (!window.isSecureContext || !navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      status.textContent = "Voice capture needs HTTPS (or localhost) — type the report instead.";
      return;
    }

    var stream = null;
    var audioCtx = null;
    var processor = null;
    var chunks = [];
    var elapsed = 0;
    var ticker = null;
    var submitting = false;
    var starting = false;
    var hostForm = widget.closest ? widget.closest("form") : null;

    function setIdle() {
      button.disabled = false;
      button.textContent = "● Record voice note";
    }

    function hint(message) {
      status.textContent = message;
      setIdle();
    }

    function teardown() {
      if (ticker) { clearInterval(ticker); ticker = null; }
      if (processor) { try { processor.disconnect(); } catch (e) { /* noop */ } processor = null; }
      if (audioCtx) { try { audioCtx.close(); } catch (e) { /* noop */ } audioCtx = null; }
      if (stream) { stream.getTracks().forEach(function (t) { t.stop(); }); stream = null; }
    }

    function flatten(parts) {
      var total = 0;
      parts.forEach(function (p) { total += p.length; });
      var out = new Float32Array(total);
      var at = 0;
      parts.forEach(function (p) { out.set(p, at); at += p.length; });
      return out;
    }

    function stop() {
      if (!audioCtx && !stream) return; // nothing running
      var samples = flatten(chunks);
      chunks = [];
      teardown();
      if (!samples.length) { hint("Nothing was captured — try again or type the report."); return; }
      submitting = true;
      button.disabled = true;
      button.textContent = "Processing…";
      status.textContent = "Transcribing and extracting the incident — this takes a few seconds.";
      try {
        submitWav(new Blob([encodeWav(samples)], { type: "audio/wav" }), hostForm);
      } catch (err) {
        submitting = false;
        hint("Could not submit the recording — type the report instead.");
      }
    }

    function start() {
      if (starting) return; // permission request in flight; a second click
      starting = true;      // must not orphan a live mic stream
      navigator.mediaDevices.getUserMedia({ audio: true }).then(function (mic) {
        starting = false;
        if (stream) { // raced an earlier resolution — stop the duplicate
          mic.getTracks().forEach(function (t) { t.stop(); });
          return;
        }
        stream = mic;
        var Ctor = window.AudioContext || window.webkitAudioContext;
        audioCtx = new Ctor({ sampleRate: SAMPLE_RATE });
        var source = audioCtx.createMediaStreamSource(stream);
        processor = audioCtx.createScriptProcessor(CHUNK, 1, 1);
        chunks = [];
        processor.onaudioprocess = function (event) {
          chunks.push(new Float32Array(event.inputBuffer.getChannelData(0)));
        };
        source.connect(processor);
        processor.connect(audioCtx.destination); // required for onaudioprocess to fire

        elapsed = 0;
        button.textContent = "■ Stop & send";
        status.textContent = "Recording — 0:00 / 2:00";
        ticker = setInterval(function () {
          elapsed += 1;
          status.textContent = "Recording — " + fmtClock(elapsed) + " / 2:00";
          if (elapsed >= MAX_SECONDS) stop(); // auto-stop + submit at the cap
        }, 1000);
      }).catch(function () {
        starting = false;
        hint("Microphone unavailable or blocked — type the report instead.");
      });
    }

    button.addEventListener("click", function () {
      if (submitting) return;
      if (stream) { stop(); } else { start(); }
    });

    setIdle();
  }

  function boot() {
    var widgets = document.querySelectorAll("[data-voice-widget]");
    for (var i = 0; i < widgets.length; i++) init(widgets[i]);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
