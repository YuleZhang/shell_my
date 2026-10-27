// yuutils native bridge v1.5.3. No transcripts, prompts or credentials on disk.
import fs from 'node:fs';
import path from 'node:path';
import http from 'node:http';
import crypto from 'node:crypto';

const hex = (n) => crypto.randomBytes(n).toString('hex');
const now = () => performance.now();

export class RecoveryGate {
  constructor(clock = now) {
    this.clock = clock;
    this.lastAttempt = -Infinity;
    this.reset();
  }
  reset() {
    this.candidate = null;
    this.settled = false;
    this.changed = this.clock();
    this.modal = false;
  }
  cancel() {
    this.candidate = null;
    this.settled = false;
    this.changed = this.clock();
  }
  end(event) {
    this.cancel();
    const last = [...(event.messages || [])].reverse().find(m => m.role === 'assistant');
    if (!last || last.stopReason !== 'error' || typeof last.errorMessage !== 'string') return;
    // Only the last native error, never transcript/terminal/history matching.
    // Explicit Key or budget-pool exhaustion wins over transient text.
    if (/OpenAI\s+API\s+error\s*\(402\)/i.test(last.errorMessage) &&
        (/API\s+Key\s+quota\s+has\s+been\s+exhausted\b/i.test(last.errorMessage) ||
         /\b(?:api_key_quota_exhausted|quota_unavailable|insufficient_quota)\b/i.test(last.errorMessage) ||
         /No\s+budget\s+pool\s+with\s+available\s+quota\s+could\s+be\s+selected\b/i.test(last.errorMessage) ||
         /No\s+budget\s+pool\s+bound\s+to\s+this\s+API\s+Key\s+currently\s+has\s+available\s+quota\b/i.test(last.errorMessage))) {
      this.candidate = 'quota-exhausted';
    } else if (/Our\s+servers\s+are\s+currently\s+overloaded\.\s*Please\s+try\s+again\s+later\./i.test(last.errorMessage)) {
      this.candidate = 'overload';
    } else if (/\brate_limit_exceeded\b/.test(last.errorMessage) ||
               /OpenAI\s+API\s+error\s*\(429\):[\s\S]*Rate\s+limit\s+exceeded\b/i.test(last.errorMessage)) {
      this.candidate = 'rate-limit';
    }
  }
  settle() {
    this.settled = true;
    this.changed = this.clock();
  }
  status(ctx, safety = null) {
    const denied = recovery => ({ recovery, eligible: false });
    // Native getEditorText() cannot prove absence of image-only drafts, host
    // overlays, custom editor state or pending terminal input. No opt-in bypass.
    if (this.candidate === 'quota-exhausted') return denied('quota-exhausted');
    if (!safety || safety.verified !== true) return denied('ui-safety-unavailable');
    if (!this.candidate) return denied('no-candidate');
    if (!this.settled) return denied('not-settled');
    try {
      if (typeof ctx.isIdle !== 'function' || !ctx.isIdle()) return denied('busy');
      if (typeof ctx.hasPendingMessages !== 'function' || ctx.hasPendingMessages()) return denied('pending');
      if (typeof ctx.ui?.getEditorText !== 'function' || ctx.ui.getEditorText() !== '') return denied('draft');
      if (this.modal || safety.modal !== false) return denied('modal');
      if (safety.images !== false || safety.draft !== false) return denied('draft');
      if (safety.input !== false) return denied('input');
      if (this.clock() - this.changed < 3000) return denied('quiet');
      if (this.clock() - this.lastAttempt < 60000) return denied('cooldown');
      return { recovery: 'eligible', eligible: true };
    } catch { return denied('unknown'); }
  }
  recover(pi, ctx, safety = null) {
    const status = this.status(ctx, safety);
    if (!status.eligible) return status;
    // Consume before fire-and-forget call; never retry an unconfirmed submission.
    this.cancel();
    this.lastAttempt = this.clock();
    try { pi.sendUserMessage('continue', { deliverAs: 'followUp' }); }
    catch { /* delivery cannot be confirmed, so leave consumed */ }
    return { recovery: 'submitted-unconfirmed', eligible: false };
  }
}

function identity() {
  const uid = process.getuid();
  if (uid !== process.geteuid() || ['SUDO_UID', 'SUDO_USER', 'SUDO_COMMAND'].some(k => k in process.env)) throw Error('UID');
  const login = fs.readFileSync('/proc/self/loginuid', 'utf8').trim();
  if (login !== String(uid) && login !== '4294967295') throw Error('login UID');
  const ids = fs.readFileSync('/proc/self/status', 'utf8').match(/^Uid:\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)/m);
  if (!ids || ids.slice(1).some(id => Number(id) !== uid)) throw Error('UID');
  const fields = fs.readFileSync('/proc/self/stat', 'utf8').split(')').slice(1).join(')').trim().split(/\s+/);
  return { uid, pid: process.pid, start: fields[19] };
}

function privateRoot(uid) {
  const root = process.env.YUUTILS_WATCH_RUNTIME || `/tmp/yuutils-watch-${uid}`;
  if (!path.isAbsolute(root) || Buffer.byteLength(root) > 65) throw Error('runtime');
  let parent = root;
  while (true) {
    if (fs.existsSync(parent) || (() => { try { fs.lstatSync(parent); return true; } catch { return false; } })()) {
      if (fs.lstatSync(parent).isSymbolicLink()) throw Error('symlink');
    }
    const next = path.dirname(parent);
    if (next === parent) break;
    parent = next;
  }
  fs.mkdirSync(root, { recursive: true, mode: 0o700 });
  const info = fs.lstatSync(root);
  if (!info.isDirectory() || info.uid !== uid || (info.mode & 0o777) !== 0o700) throw Error('private runtime');
  return root;
}

// This ELF's bundled implementation was inspected and exercised by the isolated
// TUI smoke. Unknown native builds/layouts fail closed, not by user opt-in flags.
const NATIVE_SHA256 = 'da9218ed201419a93cd233adb203743657fe497fee15117e768d2c9438dd3ae5';
export class NativeUISafety {
  constructor(ctx) {
    this.ctx = ctx;
    this.touched = false;
    this.installed = false;
  }
  async install() {
    const ui = this.ctx.ui;
    if (crypto.createHash('sha256').update(fs.readFileSync(process.execPath)).digest('hex') !== NATIVE_SHA256 ||
        typeof ui?.getEditorComponent !== 'function' || typeof ui?.setEditorComponent !== 'function' ||
        typeof ui?.custom !== 'function' || typeof ui?.onTerminalInput !== 'function' ||
        ui.getEditorComponent() !== undefined) return;
    // An immediately closed overlay factory exposes TUI without mounting UI or
    // moving focus. Inspect before the native setter clears its editor container.
    await ui.custom((tui, _theme, _keys, done) => {
      this.tui = tui;
      done();
      return { render: () => [], invalidate() {} };
    }, { overlay: true });
    const current = this.tui?.getFocusedComponent?.();
    if (this.tui?.hasOverlayEntries !== false || this.tui.hasOverlay?.() !== false ||
        !['StepEditor', 'CustomEditor'].includes(current?.constructor?.name) ||
        ui.getEditorComponent() !== undefined) return;
    // Observe the existing editor without invoking the destructive native setter.
    this.editor = current;
    this.setTextDescriptor = Object.getOwnPropertyDescriptor(current, 'setText');
    const original = current.setText;
    if (typeof original !== 'function') return;
    // Native resetExtensionUI (before session_shutdown on /reload) redundantly
    // sets the same text, clearing compressed pastes, cursor/history and undo.
    // Preserve that no-op; genuine text replacements keep native semantics.
    this.setText = function(text) {
      if (text === this.getText()) return;
      return original.call(this, text);
    };
    current.setText = this.setText;
    this.installed = true;
  }
  snapshot(ctx = this.ctx) {
    try {
      const e = this.editor, t = this.tui;
      if (!this.installed || ctx.ui?.getEditorComponent?.() !== undefined ||
          e?.setText !== this.setText ||
          typeof t?.getFocusedComponent !== 'function' || typeof t.hasOverlay !== 'function' ||
          typeof t.hasOverlayEntries !== 'boolean' || !Array.isArray(t.overlayStack) ||
          typeof e?.getText !== 'function' || typeof e.getExpandedText !== 'function' ||
          typeof e.isInPaste !== 'boolean' || typeof e.pasteBuffer !== 'string' ||
          !(e.pastes instanceof Map) || typeof e.isShowingAutocomplete !== 'function') return null;
      const text = e.getExpandedText();
      if (typeof text !== 'string') return null;
      // This native build resolves images only through [Image #n] in editor text;
      // orphaned registry entries are not submitted. Any terminal input remains
      // latched until a new turn, covering asynchronous clipboard/path insertion.
      return { verified: true, modal: t.hasOverlayEntries || t.hasOverlay() ||
                 t.getFocusedComponent() !== e || e.isShowingAutocomplete(),
               images: /\[Image #\d+\]/.test(text), draft: e.getText() !== '' || text !== '',
               input: this.touched || e.isInPaste || e.pasteBuffer !== '' };
    } catch { return null; }
  }
  close() {
    // Restore only our own method; never invoke a native editor setter on cleanup.
    if (this.editor && this.editor.setText === this.setText) {
      if (this.setTextDescriptor) Object.defineProperty(this.editor, 'setText', this.setTextDescriptor);
      else delete this.editor.setText;
    }
    this.installed = false;
  }
}

export default function bridge(pi) {
  const gate = new RecoveryGate();
  let context, server, files = [], unsubscribe, uiSafety;
  const close = async () => {
    uiSafety?.close();
    uiSafety = undefined;
    gate.reset();
    unsubscribe?.();
    unsubscribe = undefined;
    if (server) {
      const old = server;
      server = undefined;
      old.closeAllConnections?.();
      await new Promise(resolve => old.close(resolve));
    }
    for (const file of files) {
      try { fs.unlinkSync(file); } catch { /* own unique files only */ }
    }
    files = [];
  };
  pi.on('session_start', async (_event, ctx) => {
    await close();
    context = ctx;
    try {
      const { uid, pid, start } = identity();
      uiSafety = new NativeUISafety(ctx);
      await uiSafety.install();
      const root = privateRoot(uid);
      const generation = hex(16), cap = hex(32), nonces = new Map();
      const basename = `b-${pid}-${generation}`;
      const socket = path.join(root, basename + '.sock');
      const metadata = path.join(root, basename + '.json');
      server = http.createServer((req, res) => {
        const reply = (code, value = {}) => {
          res.writeHead(code, { 'Content-Type': 'application/json' });
          res.end(JSON.stringify(value));
        };
        if (req.method !== 'POST' || req.url !== '/' || req.headers['transfer-encoding'] ||
            !/^\d+$/.test(req.headers['content-length'] || '') || Number(req.headers['content-length']) > 2048) {
          req.resume(); reply(400); return;
        }
        let body = '', bytes = 0;
        req.on('data', chunk => {
          bytes += chunk.length;
          if (bytes > 2048) req.destroy(); else body += chunk;
        });
        req.on('end', () => {
          try {
            const request = JSON.parse(body);
            const keys = Object.keys(request).sort().join(',');
            if (keys !== 'cap,generation,nonce,op' || typeof request.cap !== 'string' ||
                request.cap.length !== cap.length ||
                !crypto.timingSafeEqual(Buffer.from(request.cap), Buffer.from(cap)) ||
                request.generation !== generation || !/^[0-9a-f]{32}$/.test(request.nonce) ||
                nonces.has(request.nonce) || !['status', 'recover'].includes(request.op)) {
              reply(403); return;
            }
            const timestamp = Number.parseInt(request.nonce.slice(0, 12), 16);
            const time = Date.now();
            if (timestamp < time - 30000 || timestamp > time + 2000) { reply(403); return; }
            // Keep every nonce for its whole validity window, including future skew.
            for (const [nonce, expires] of nonces) if (expires < time) nonces.delete(nonce);
            if (nonces.size >= 4096) { reply(503); return; }
            nonces.set(request.nonce, timestamp + 30000);
            const safety = uiSafety?.snapshot(context);
            reply(200, request.op === 'status' ? gate.status(context, safety) : gate.recover(pi, context, safety));
          } catch { reply(400); }
        });
      });
      server.requestTimeout = 1500;
      server.headersTimeout = 1500;
      server.on('connection', socket => socket.setTimeout(1500, () => socket.destroy()));
      await new Promise((resolve, reject) => {
        server.once('error', reject);
        server.listen(socket, resolve);
      });
      files.push(socket);
      fs.chmodSync(socket, 0o600);
      const fd = fs.openSync(metadata, fs.constants.O_WRONLY | fs.constants.O_CREAT |
                             fs.constants.O_EXCL | fs.constants.O_NOFOLLOW, 0o600);
      files.push(metadata);
      try { fs.writeFileSync(fd, JSON.stringify({ uid, pid, start, generation, cap })); }
      finally { fs.closeSync(fd); }
      if (typeof ctx.ui?.onTerminalInput === 'function') {
        unsubscribe = ctx.ui.onTerminalInput(() => {
          gate.cancel();
          if (uiSafety) uiSafety.touched = true;
        });
      }
    } catch {
      await close();
      // Fixed diagnostic only. Never emit error text or private capability.
      ctx.ui?.setStatus?.('yuutils-watch', 'watch bridge unavailable');
    }
  });
  pi.on('agent_end', (event, ctx) => { context = ctx; gate.end(event); });
  pi.on('agent_settled', (_event, ctx) => { context = ctx; gate.settle(); });
  pi.on('agent_start', () => {
    gate.cancel();
    if (uiSafety) uiSafety.touched = false;
  });
  for (const event of ['input', 'before_agent_start', 'tool_execution_start',
                        'session_before_switch', 'session_before_fork', 'session_before_compact']) {
    pi.on(event, () => { gate.cancel(); });
  }
  pi.on('ui_prompt_start', () => { gate.cancel(); gate.modal = true; });
  pi.on('ui_prompt_end', () => { gate.cancel(); gate.modal = false; });
  pi.on('session_shutdown', close);
}
